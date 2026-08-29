"""Windows 人工操作检测。

只有前台窗口属于脚本当前控制的 Windows 模拟器实例时，才把系统最近输入
视为人工接管，避免其他模拟器或管理进程的输入暂停调度器。
"""

import ctypes
import ntpath
import re
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

    SUPPORTED_EMULATOR_TYPES = frozenset({
        'NoxPlayer',
        'NoxPlayer64',
        'BlueStacks4',
        'BlueStacks5',
        'BlueStacks4HyperV',
        'BlueStacks5HyperV',
        'MuMuPlayer',
        'MuMuPlayerX',
        'MuMuPlayer12',
        'MEmuPlayer',
    })

    # 目标配置可能来自多实例管理器，但人工接管只能归因到真正承载游戏窗口
    # 的播放器进程。管理器、服务进程和主页进程即使路径相同，也不能进入白名单。
    PROCESS_NAMES = {
        'NoxPlayer': frozenset({'nox.exe'}),
        'NoxPlayer64': frozenset({'nox.exe'}),
        'BlueStacks4': frozenset({'bluestacks.exe'}),
        'BlueStacks4HyperV': frozenset({'bluestacks.exe'}),
        'BlueStacks5': frozenset({'hd-player.exe'}),
        'BlueStacks5HyperV': frozenset({'hd-player.exe'}),
        'MuMuPlayer': frozenset({'nemuplayer.exe'}),
        'MuMuPlayerX': frozenset({'nemuplayer.exe'}),
        'MuMuPlayer12': frozenset({'mumuplayer.exe'}),
        'MEmuPlayer': frozenset({'memu.exe'}),
    }

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
        # 不能使用 os.path：在 Ubuntu CI 上它会按 POSIX 规则解释 Windows 路径。
        return ntpath.normcase(ntpath.normpath(str(path))).replace('\\', '/')

    @classmethod
    def _target_process_paths(cls, emulator_type, target_path):
        """返回目标实例允许匹配的播放器路径。"""
        target_path = str(target_path)
        target_name = ntpath.basename(target_path).casefold()
        if emulator_type == 'MuMuPlayer12' and target_name == 'mumunxmain.exe':
            # MuMuNxMain.exe 是主页/管理器；只沿固定目录关系寻找播放器。
            target_directory = ntpath.dirname(target_path)
            parent_directory = ntpath.dirname(target_directory)
            return {
                cls._normalize_path(ntpath.join(directory, 'MuMuPlayer.exe'))
                for directory in {
                    target_directory,
                    parent_directory,
                    ntpath.join(parent_directory, 'EmulatorShell'),
                }
            }

        allowed_names = cls.PROCESS_NAMES.get(emulator_type, frozenset())
        if target_name not in allowed_names:
            return set()
        return {cls._normalize_path(target_path)}

    @staticmethod
    def _title_matches_instance(window_title, instance_name):
        """按窗口标题边界匹配实例名，避免 1 命中 10 或 100。"""
        # 模拟器实例名通常包含连字符；将其视为 token 字符的一部分，避免
        # MuMuPlayer-12.0-1 命中 MuMuPlayer-12.0-10。
        pattern = rf'(?<![\w-]){re.escape(instance_name)}(?![\w-])'
        return re.search(pattern, window_title) is not None

    @classmethod
    def _command_line_matches_instance(cls, emulator_type, command_line, instance_name):
        """解析家族特定的启动参数；返回 True、False 或未知 None。"""
        if command_line is None:
            return None
        args = [str(value).strip().casefold() for value in command_line]
        if not args:
            return None

        if emulator_type == 'MuMuPlayer':
            return True if not instance_name else instance_name in args[1:]
        if not instance_name:
            return False

        if emulator_type in {'BlueStacks5', 'BlueStacks5HyperV'}:
            option = '--instance'
            positions = [index for index, value in enumerate(args) if value == option]
            if not positions:
                return None
            return any(
                index + 1 < len(args) and args[index + 1] == instance_name
                for index in positions
            )

        if emulator_type in {'BlueStacks4', 'BlueStacks4HyperV'}:
            option = '-vmname'
            positions = [index for index, value in enumerate(args) if value == option]
            if not positions:
                return None
            return any(
                index + 1 < len(args) and args[index + 1] == instance_name
                for index in positions
            )

        if emulator_type in {'NoxPlayer', 'NoxPlayer64'}:
            prefix = '-clone:'
            clone_args = [value for value in args if value.startswith(prefix)]
            if not clone_args:
                return None
            return any(value == f'{prefix}{instance_name}' for value in clone_args)

        if emulator_type == 'MEmuPlayer':
            # MEmu 的启动形式是 MEmu.exe <name>，name 必须是精确位置参数。
            return len(args) > 1 and args[1] == instance_name

        if emulator_type == 'MuMuPlayerX':
            option = '-m'
            positions = [index for index, value in enumerate(args) if value == option]
            if not positions:
                return None
            return any(
                index + 1 < len(args) and args[index + 1] == instance_name
                for index in positions
            )

        if emulator_type == 'MuMuPlayer12':
            # MuMu12 的播放器参数在不同版本中略有差异；只接受完整实例名 token。
            return instance_name in args[1:]

        return None

    @classmethod
    def _process_matches_instance(cls, process, hwnd, emulator_instance):
        """判断前台进程是否属于脚本当前控制的模拟器实例。"""
        emulator_type = getattr(emulator_instance, 'type', '')
        if emulator_type not in cls.SUPPORTED_EMULATOR_TYPES:
            return False

        target_path = getattr(emulator_instance, 'path', '')
        if not target_path:
            return False

        try:
            process_path = cls._normalize_path(process.exe())
            target_paths = cls._target_process_paths(emulator_type, target_path)
            if process_path not in target_paths:
                return False

            instance_name = str(getattr(emulator_instance, 'name', '') or '').strip().casefold()
        except (psutil.Error, OSError):
            return False

        try:
            command_line = process.cmdline()
        except (psutil.Error, OSError, AttributeError, TypeError):
            command_line = None
        command_match = cls._command_line_matches_instance(
            emulator_type, command_line, instance_name
        )
        if command_match is not None:
            return command_match

        try:
            window_title = cls._window_title(hwnd).casefold()
        except (AttributeError, OSError, ValueError):
            window_title = ''
        return bool(instance_name and cls._title_matches_instance(window_title, instance_name))

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
