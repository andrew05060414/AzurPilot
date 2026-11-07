"""调度器单实例锁：同一配置只允许一个调度器进程运行。"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from alas import AzurLaneAutoScript
from module.runtime import scheduler_lock
from module.runtime.scheduler_lock import (
    SchedulerLockConflict,
    _lock_file_is_current,
    acquire_scheduler_lock,
    release_scheduler_lock,
)


class TestSchedulerLockLocation(unittest.TestCase):
    def test_lock_dir_is_outside_cleanable_cache(self):
        self.assertEqual(scheduler_lock.SCHEDULER_LOCK_DIR, Path('./config/.locks/scheduler'))
        self.assertNotIn('cache', scheduler_lock.SCHEDULER_LOCK_DIR.parts)


class TestSchedulerLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(scheduler_lock, 'SCHEDULER_LOCK_DIR', Path(self.tmp.name)))
        self.enterContext(patch.object(scheduler_lock, 'SCHEDULER_LOCK_RETRY_INTERVAL', 0.05))

    def test_second_holder_conflicts_until_first_releases(self):
        first = acquire_scheduler_lock('alas')
        with self.assertRaises(SchedulerLockConflict):
            acquire_scheduler_lock('alas', timeout=0.2)

        # 不同配置互不影响
        other = acquire_scheduler_lock('ap', timeout=0.2)
        release_scheduler_lock(other)

        release_scheduler_lock(first)
        release_scheduler_lock(acquire_scheduler_lock('alas', timeout=0.2))

    def test_lock_is_released_when_holder_process_is_killed(self):
        code = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            from module.runtime import scheduler_lock
            scheduler_lock.SCHEDULER_LOCK_DIR = Path({self.tmp.name!r})
            handle = scheduler_lock.acquire_scheduler_lock('alas')
            print('locked', flush=True)
            time.sleep(60)
        """)
        child = subprocess.Popen(
            [sys.executable, '-c', code],
            stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(child.kill)
        # 导入模块时 logger 会先输出启动横幅
        for line in child.stdout:
            if line.strip() == 'locked':
                break
        else:
            self.fail('子进程未能取得调度器锁')

        with self.assertRaises(SchedulerLockConflict):
            acquire_scheduler_lock('alas', timeout=0.3)

        # 强制结束持有者（如任务管理器结束进程）后，锁由系统释放
        child.kill()
        child.wait(timeout=10)
        release_scheduler_lock(acquire_scheduler_lock('alas', timeout=5))

    def test_replaced_lock_file_is_reacquired(self):
        # 模拟首次加锁后路径已被替换：应丢弃句柄并重新取锁，而不是直接返回
        with patch.object(scheduler_lock, '_lock_file_is_current', side_effect=[False, True]) as check:
            handle = acquire_scheduler_lock('alas', timeout=1)
        self.assertEqual(check.call_count, 2)
        with self.assertRaises(SchedulerLockConflict):
            acquire_scheduler_lock('alas', timeout=0.2)
        release_scheduler_lock(handle)

    @unittest.skipIf(os.name == 'nt', 'Windows 下已打开的锁文件不能被删除')
    def test_lock_file_removed_after_locking_is_recreated(self):
        lock_path = Path(self.tmp.name) / 'alas.lock'
        real_try_lock = scheduler_lock._try_lock
        removed = []

        def lock_then_remove(handle):
            real_try_lock(handle)
            if not removed:
                removed.append(True)
                lock_path.unlink()

        with patch.object(scheduler_lock, '_try_lock', side_effect=lock_then_remove):
            handle = acquire_scheduler_lock('alas', timeout=1)
        self.assertEqual(len(removed), 1)
        # 返回的句柄必须是路径上现存的文件，且之后的竞争者会被正确拒绝
        self.assertEqual(os.fstat(handle.fileno()).st_ino, os.stat(lock_path).st_ino)
        with self.assertRaises(SchedulerLockConflict):
            acquire_scheduler_lock('alas', timeout=0.2)
        release_scheduler_lock(handle)

    @unittest.skipIf(os.name == 'nt', 'Windows 下 _lock_file_is_current 恒为 True')
    def test_identity_check_detects_replaced_or_missing_file(self):
        lock_path = Path(self.tmp.name) / 'alas.lock'
        with open(lock_path, 'a+b') as handle:
            self.assertTrue(_lock_file_is_current(handle, lock_path))
            lock_path.unlink()
            self.assertFalse(_lock_file_is_current(handle, lock_path))
            lock_path.write_bytes(b'\0')
            self.assertFalse(_lock_file_is_current(handle, lock_path))


class TestLoopUsesSchedulerLock(unittest.TestCase):
    def make_script(self):
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)
        script.config_name = 'test'
        script._scheduler_lock = None
        script._loop = Mock(return_value='done')
        return script

    def test_conflict_exits_without_running_scheduler(self):
        script = self.make_script()
        with (
            patch('alas.logger'),
            patch('module.runtime.scheduler_lock.acquire_scheduler_lock',
                  side_effect=SchedulerLockConflict('busy')),
        ):
            with self.assertRaises(SystemExit) as caught:
                script.loop()
        self.assertEqual(caught.exception.code, 1)
        script._loop.assert_not_called()

    def test_lock_is_released_after_loop_returns_or_raises(self):
        for outcome in ('done', RuntimeError('boom')):
            with self.subTest(outcome=outcome):
                script = self.make_script()
                handle = Mock()
                if isinstance(outcome, Exception):
                    script._loop.side_effect = outcome
                with (
                    patch('alas.logger'),
                    patch('module.runtime.scheduler_lock.acquire_scheduler_lock', return_value=handle),
                    patch('module.runtime.scheduler_lock.release_scheduler_lock') as release,
                ):
                    if isinstance(outcome, Exception):
                        with self.assertRaises(RuntimeError):
                            script.loop()
                    else:
                        self.assertEqual(script.loop(), 'done')
                release.assert_called_once_with(handle)
                self.assertIsNone(script._scheduler_lock)

    def test_lock_file_error_does_not_block_start(self):
        script = self.make_script()
        with (
            patch('alas.logger'),
            patch('module.runtime.scheduler_lock.acquire_scheduler_lock', side_effect=OSError('read-only')),
        ):
            self.assertEqual(script.loop(), 'done')


if __name__ == '__main__':
    unittest.main()
