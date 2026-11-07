"""托盘「暂停 / 继续」调度器端点的单元测试。"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from module.api import launcher_api
from module.runtime import launcher_trust

SECRET = 'tray-secret'


class FakeRequest:
    """只带处理器会读到的属性的请求替身。"""

    def __init__(self, method='GET', body=None, secret=SECRET, host='127.0.0.1'):
        self.method = method
        self.client = SimpleNamespace(host=host)
        self.headers = {'host': '127.0.0.1:27548'}
        if secret is not None:
            self.headers['x-webui-launcher-secret'] = secret
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError('not json')
        return self._body


class FakeRuntime:
    """记录 start / stop 调用，并维护一份假的运行集合。"""

    def __init__(self, running, names):
        self.running = set(running)
        self.calls = []
        self.configs = SimpleNamespace(names=lambda: list(names))

    def stop(self, name):
        self.calls.append(('stop', name))
        self.running.discard(name)

    def start(self, name):
        self.calls.append(('start', name))
        self.running.add(name)


class TestSchedulerEndpoint(unittest.TestCase):
    def setUp(self):
        launcher_trust._reset()
        launcher_trust.configure(SECRET, 'pw')
        self.runtime = FakeRuntime(running=['alas'], names=['alas', 'alt'])
        launcher_api.configure_runtime(self.runtime)
        launcher_api._paused = []
        patcher = mock.patch.object(launcher_api, '_running_names', lambda: sorted(self.runtime.running))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        launcher_trust._reset()
        launcher_api.configure_runtime(None)
        launcher_api._paused = []

    def call(self, request):
        response = asyncio.run(launcher_api.scheduler(request))
        return response.status_code, json.loads(bytes(response.body))

    def test_requires_launcher_secret(self):
        """没有或带错信任密钥一律 403，网页跨站请求无法暂停调度器。"""
        for secret in (None, 'wrong'):
            status, _ = self.call(FakeRequest(method='POST', body={'action': 'pause'}, secret=secret))
            self.assertEqual(status, 403)
        self.assertEqual(self.runtime.calls, [])

    def test_remote_request_rejected(self):
        status, _ = self.call(FakeRequest(host='10.0.0.2'))
        self.assertEqual(status, 403)

    def test_invalid_action(self):
        status, _ = self.call(FakeRequest(method='POST', body={'action': 'reboot'}))
        self.assertEqual(status, 400)

    def test_pause_then_resume_restores_same_instances(self):
        """暂停记住正在跑的实例，继续只恢复这些实例。"""
        status, payload = self.call(FakeRequest(method='POST', body={'action': 'pause'}))
        self.assertEqual(status, 200)
        self.assertEqual(payload['running'], [])
        self.assertEqual(payload['paused'], ['alas'])

        status, payload = self.call(FakeRequest(method='POST', body={'action': 'resume'}))
        self.assertEqual(status, 200)
        self.assertEqual(payload['running'], ['alas'])
        self.assertEqual(payload['paused'], [])
        self.assertEqual(self.runtime.calls, [('stop', 'alas'), ('start', 'alas')])

    def test_resume_without_memory_starts_all(self):
        """后端重启后没有暂停记录，继续就启动全部实例。"""
        self.runtime.running.clear()
        status, payload = self.call(FakeRequest(method='POST', body={'action': 'resume'}))
        self.assertEqual(status, 200)
        self.assertEqual(payload['running'], ['alas', 'alt'])

    def test_get_reports_state(self):
        status, payload = self.call(FakeRequest())
        self.assertEqual(status, 200)
        self.assertEqual(payload['running'], ['alas'])


if __name__ == '__main__':
    unittest.main()
