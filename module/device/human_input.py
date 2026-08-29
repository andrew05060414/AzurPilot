"""Windows 人工操作检测。

仅在前台窗口属于脚本当前控制的模拟器实例时，才把系统最近输入视为人工接管，
避免其他模拟器或模拟器管理进程的输入暂停调度器。
"""

import ctypes
import os
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

    @staticmethod
    def _normalize_path(path):
        """统一 Windows 进程路径和模拟器配置路径的格式。"""
        return os.path.normcase(os.path.abspath(path)).replace('\\', '/')

    @classmethod
    def _process_matches_instance(cls, process, hwnd, emulator_instance):
        """判断前台进程是否属于脚本当前控制的模拟器实例。"""
        target_path = getattr(emulator_instance, 'path', '')
        if not target_path:
            return False

        try:
            if cls._normalize_path(process.exe()) != cls._normalize_path(target_path):
                return False

            # 多实例模拟器通常共用同一个 exe，必须再匹配实例名。
            instance_name = str(getattr(emulator_instance, 'name', '') or '').strip().casefold()
            if not instance_name:
                return True

            command_line = ' '.join(str(value) for value in process.cmdline()).casefold()
        except (psutil.Error, OSError):
            return False

        try:
            window_title = cls._window_title(hwnd).casefold()
        except (OSError, ValueError):
            window_title = ''
        return instance_name in command_line or instance_name in window_title

    @classmethod
    def _window_title(cls, hwnd):
        """返回窗口标题；读取失败时返回空字符串。"""
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ''
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    @classmethod
    def foreground_emulator_process_id(cls, emulator_instance=None):
        """返回脚本目标模拟器的前台进程 ID；否则返回 ``None``。"""
        if emulator_instance is None:
            return None

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None

        process_id = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return None

        try:
            process = psutil.Process(process_id.value)
        except (psutil.Error, OSError):
            return None
        if not cls._process_matches_instance(process, hwnd, emulator_instance):
            return None
        return process_id.value

    @classmethod
    def foreground_is_emulator(cls, emulator_instance=None):
        """当前前台窗口是否属于脚本目标模拟器实例。"""
        return cls.foreground_emulator_process_id(emulator_instance) is not None

    @classmethod
    def wait_until_idle(cls, idle_timeout, emulator_instance=None):
        """在用户停止操作目标模拟器 ``idle_timeout`` 秒后返回。"""
        process_id = cls.foreground_emulator_process_id(emulator_instance)
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
            process_id = cls.foreground_emulator_process_id(emulator_instance)
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


def wait_for_human_input_idle(config, device=None):
    """按配置在 Windows 上等待用户释放模拟器控制权。"""
    if not IS_WINDOWS or not getattr(config, 'Optimization_PauseOnUserInput', False):
        return

    if device is None:
        return
    emulator_instance = getattr(device, 'emulator_instance', None)
    if emulator_instance is None:
        return

    idle_timeout = getattr(config, 'Optimization_UserInputIdleTimeout', 30)
    if idle_timeout <= 0:
        return
    WindowsHumanInputMonitor.wait_until_idle(idle_timeout, emulator_instance)
