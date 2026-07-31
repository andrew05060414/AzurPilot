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

    EMULATOR_PROCESS_NAMES = frozenset({
        'bluestacks.exe',
        'bluestacksgp.exe',
        'dnplayer.exe',
        'hd-player.exe',
        'ldplayer.exe',
        'memu.exe',
        'memuheadless.exe',
        'mumunxmain.exe',
        'mumuplayer.exe',
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
    def foreground_is_emulator(cls):
        """当前前台窗口是否属于受支持模拟器的进程。"""
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False

        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return False

        try:
            process_name = psutil.Process(process_id.value).name().lower()
        except (psutil.Error, OSError):
            return False
        return process_name in cls.EMULATOR_PROCESS_NAMES

    @classmethod
    def wait_until_idle(cls, idle_timeout):
        """在用户停止操作模拟器 ``idle_timeout`` 秒后返回。"""
        if not cls.foreground_is_emulator():
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
            if not cls.foreground_is_emulator():
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
