"""Windows 人工操作检测。

仅在前台窗口属于已知安卓模拟器进程时，才把系统最近输入视为人工接管，
避免用户在其他应用中输入时无故暂停调度器。
"""

import ctypes
import time

import psutil

from module.device.env import IS_WINDOWS
from module.logger import logger


class _LastInputInfo(ctypes.Structure):
    """Windows ``GetLastInputInfo`` 所需的数据结构。"""

    _fields_ = [
        ('cbSize', ctypes.c_uint),
        ('dwTime', ctypes.c_uint),
    ]


class WindowsHumanInputMonitor:
    """检测用户是否正在操作 Windows 模拟器窗口。"""

    # ``GetLastInputInfo`` 是整个 Windows 会话级别的时间戳。记录最近一次
    # 观测到的模拟器进程，避免把切回模拟器前在其他窗口中的输入归因给模拟器。
    _last_emulator_process_id = None

    EMULATOR_PROCESS_NAMES = frozenset({
        'bluestacks.exe',
        'bluestacksgp.exe',
        'dnplayer.exe',
        'hd-player.exe',
        'ldplayer.exe',
        'memu.exe',
        'memuheadless.exe',
        'mumunxdevice.exe',
        'mumunxmain.exe',
        'mumunxservice.exe',
        'mumunxsvc.exe',
        'mumuplayer.exe',
        'mumuremotebackend.exe',
        'mumuremotehealthd.exe',
        'mumuremoteservice.exe',
        'nemuplayer.exe',
        'nox.exe',
        'nox64.exe',
    })

    @classmethod
    def recent_input_seconds(cls):
        """返回距上次鼠标或键盘输入的秒数；读取失败时返回 ``None``。"""
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None

        # GetTickCount 与 dwTime 都是 32 位毫秒计数，减法按无符号数自然处理回绕。
        elapsed_ms = ctypes.c_uint32(
            ctypes.windll.kernel32.GetTickCount() - info.dwTime
        ).value
        return elapsed_ms / 1000

    @classmethod
    def foreground_emulator_process_id(cls):
        """返回当前前台受支持模拟器的进程 ID；否则返回 ``None``。"""
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None

        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return None

        try:
            process_name = psutil.Process(process_id.value).name().lower()
        except (psutil.Error, OSError):
            return None
        if process_name not in cls.EMULATOR_PROCESS_NAMES:
            return None
        return process_id.value

    @classmethod
    def foreground_is_emulator(cls):
        """当前前台窗口是否属于受支持模拟器的进程。"""
        return cls.foreground_emulator_process_id() is not None

    @classmethod
    def wait_until_idle(cls, idle_timeout):
        """在用户停止操作模拟器 ``idle_timeout`` 秒后返回。"""
        process_id = cls.foreground_emulator_process_id()
        if process_id is None:
            cls._last_emulator_process_id = None
            return

        # 前台刚切回模拟器时，当前会话级输入时间可能来自其他窗口。
        # 只建立新的观测基线，下一次检查再判断是否真的操作了模拟器。
        if process_id != cls._last_emulator_process_id:
            cls._last_emulator_process_id = process_id
            return

        elapsed = cls.recent_input_seconds()
        if elapsed is None or elapsed >= idle_timeout:
            return

        logger.info(
            f'[设备-人工接管] 检测到用户正在操作模拟器，'
            f'暂停自动控制，静止 {idle_timeout:g} 秒后继续'
        )
        while 1:
            time.sleep(0.5)
            process_id = cls.foreground_emulator_process_id()
            if process_id is None:
                cls._last_emulator_process_id = None
                break
            if process_id != cls._last_emulator_process_id:
                # 下次调用先为新前台进程建立基线，避免沿用旧模拟器的会话级输入。
                cls._last_emulator_process_id = None
                break

            elapsed = cls.recent_input_seconds()
            if elapsed is None or elapsed >= idle_timeout:
                break
        logger.info('[设备-人工接管] 用户操作结束，恢复自动控制')


def wait_for_human_input_idle(config):
    """按配置在 Windows 上等待用户释放模拟器控制权。"""
    if not IS_WINDOWS or not getattr(config, 'Optimization_PauseOnUserInput', False):
        return

    idle_timeout = getattr(config, 'Optimization_UserInputIdleTimeout', 30)
    if idle_timeout <= 0:
        return
    WindowsHumanInputMonitor.wait_until_idle(idle_timeout)
