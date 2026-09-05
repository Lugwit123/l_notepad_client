# -*- coding: utf-8 -*-
"""l_notepad 内嵌的文件夹收藏全局快捷键（Ctrl + 中键/左键）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6 import QtCore

from . import paths

BUTTON_MSGS = {
    "middle": {"down": 0x0207, "name": "中键", "vk": 0x04},
    "left": {"down": 0x0201, "name": "左键", "vk": 0x01},
}

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
GIT_HTTP_CONNECT_TIMEOUT_SEC = 5


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

logger = logging.getLogger(__name__)


def _safe_log(message: str, level: int = logging.DEBUG) -> None:
    try:
        logger.log(level, message)
    except Exception:
        pass


def _get_config_file() -> Path:
    return paths.favorites_dir() / "config.json"


def load_hotkey_button() -> str:
    """从配置文件加载快捷键按钮设置，返回 'middle' 或 'left'。"""
    try:
        button = json.loads(_get_config_file().read_text(encoding="utf-8")).get(
            "hotkey_button", "middle"
        )
    except Exception:
        button = "middle"
    return button if button in BUTTON_MSGS else "middle"


def save_hotkey_button(button: str) -> None:
    """保存快捷键按钮设置到配置文件。"""
    button = button if button in BUTTON_MSGS else "middle"
    cfg_file = _get_config_file()
    config = {}
    try:
        config = json.loads(cfg_file.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            config = {}
    except Exception:
        pass
    config["hotkey_button"] = button
    cfg_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


_on_windows = sys.platform == "win32"
if _on_windows:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
    )
    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        HOOKPROC,
        ctypes.wintypes.HINSTANCE,
        ctypes.wintypes.DWORD,
    ]
    _user32.SetWindowsHookExW.restype = ctypes.wintypes.HHOOK
    _user32.CallNextHookEx.argtypes = [
        ctypes.wintypes.HHOOK,
        ctypes.c_int,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    ]
    _user32.CallNextHookEx.restype = ctypes.c_long
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.wintypes.HHOOK]
    _user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL
    _kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE
else:
    _user32 = None
    _kernel32 = None
    HOOKPROC = None


class GlobalMouseMonitor(QtCore.QObject):
    """全局鼠标监控器，用于检测 Ctrl+中键/左键。"""

    triggered = QtCore.Signal()

    VK_LCONTROL = 0xA2
    VK_RCONTROL = 0xA3

    def __init__(self, trigger_button: str = "middle", parent=None) -> None:
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)
        self._ctrl_was_down = False
        self._poll_count = 0
        self._hook_handle = None
        self._hook_proc_ref = None
        self._last_trigger_time = 0.0
        self._last_ctrl_log_time = 0.0
        self._last_heartbeat_time = 0.0
        self._trigger_button = "middle"
        self._btn_down_msg = BUTTON_MSGS["middle"]["down"]
        self._btn_name = BUTTON_MSGS["middle"]["name"]
        self._btn_vk = BUTTON_MSGS["middle"]["vk"]
        self.set_trigger_button(trigger_button)

    def start(self) -> bool:
        """启动鼠标钩子 + Ctrl 轮询。"""
        if not _on_windows:
            _safe_log("全局鼠标快捷键仅支持 Windows", logging.WARNING)
            return False

        self._hook_proc_ref = HOOKPROC(self._mouse_ll_proc)
        h_mod = _kernel32.GetModuleHandleW(None)
        try:
            self._hook_handle = _user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._hook_proc_ref, h_mod, 0
            )
        except Exception as exc:
            self._hook_handle = None
            _safe_log(f"SetWindowsHookExW 异常: {exc}", logging.WARNING)

        if self._hook_handle:
            _safe_log(f"WH_MOUSE_LL 钩子已安装 (handle={self._hook_handle})", logging.INFO)
        else:
            err = ctypes.get_last_error() or 0
            _safe_log(f"WH_MOUSE_LL 钩子安装失败 (GetLastError={err})，仅用轮询", logging.WARNING)

        self._timer.start()
        _safe_log("Ctrl 轮询已启动 (QTimer 30ms)", logging.INFO)
        return True

    def stop(self) -> None:
        self._timer.stop()
        self._stop_hook()

    def _stop_hook(self) -> None:
        if self._hook_handle:
            try:
                _user32.UnhookWindowsHookEx(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None

    def _mouse_ll_proc(self, n_code, w_param, l_param):
        if n_code >= 0 and w_param == self._btn_down_msg:
            ctrl_down = bool(
                (_user32.GetAsyncKeyState(self.VK_LCONTROL) & 0x8000)
                or (_user32.GetAsyncKeyState(self.VK_RCONTROL) & 0x8000)
            )
            if ctrl_down:
                now = time.time()
                if now - self._last_trigger_time > 0.2:
                    self._last_trigger_time = now
                    _safe_log(f"Ctrl+{self._btn_name} 已触发", logging.INFO)
                    self.triggered.emit()
        return _user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

    def _poll(self) -> None:
        if not _on_windows:
            return
        self._poll_count += 1
        lctrl = bool(_user32.GetAsyncKeyState(self.VK_LCONTROL) & 0x8000)
        rctrl = bool(_user32.GetAsyncKeyState(self.VK_RCONTROL) & 0x8000)
        ctrl_down = lctrl or rctrl
        now = time.time()

        if ctrl_down != self._ctrl_was_down and now - self._last_ctrl_log_time > 2.0:
            self._last_ctrl_log_time = now
            side = "L" if lctrl else ("R" if rctrl else "None")
            state = "PRESSED" if ctrl_down else "RELEASED"
            _safe_log(f"Ctrl {state} (side={side})")

        if ctrl_down and not self._ctrl_was_down:
            btn_down = bool(_user32.GetAsyncKeyState(self._btn_vk) & 0x8000)
            if btn_down and now - self._last_trigger_time > 0.2:
                self._last_trigger_time = now
                _safe_log("Ctrl+鼠标 快捷键已触发（轮询备用）", logging.INFO)
                self.triggered.emit()

        if now - self._last_heartbeat_time > 60.0:
            self._last_heartbeat_time = now
            _safe_log(
                f"热键监听 heartbeat #{self._poll_count}, "
                f"ctrl={ctrl_down}, hook={'OK' if self._hook_handle else 'FAIL'}"
            )

        self._ctrl_was_down = ctrl_down

    def set_trigger_button(self, button: str) -> None:
        button = button if button in BUTTON_MSGS else "middle"
        self._trigger_button = button
        info = BUTTON_MSGS[button]
        self._btn_down_msg = info["down"]
        self._btn_name = info["name"]
        self._btn_vk = info["vk"]
        _safe_log(f"触发按钮已切换为: Ctrl+{self._btn_name}", logging.INFO)


class GlobalKeyboardWinVHotkey(QtCore.QObject):
    """低级键盘钩子：接管 Win+V（阻止系统剪贴板历史 / 开始菜单）。

    策略：Win 键按下即吞掉（系统收不到 Win，不弹开始菜单），
    Win 按住期间按下 V 则触发剪贴板历史回调并吞掉 V。
    注意：触发回调必须延迟到事件循环执行——钩子回调内同步做重活会超过
    LowLevelHooksTimeout，Windows 将直接放行该 V 按下（前台程序收到裸 V）。
    副作用：Win 单独键 / Win+其它组合键将不再触发系统功能（覆盖 Win+V 的取舍）。
    """

    triggered = QtCore.Signal()

    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    VK_V = 0x56
    VK_LWIN = 0x5B
    VK_RWIN = 0x5C
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    LLKHF_INJECTED = 0x00000010  # 注入输入标记（我们自己补发的按键会带此标志）
    # Win 按住期间应放行的修饰键（避免误触发补发；Ctrl/Shift/Alt 及左右键）
    _MODIFIER_VKS = frozenset({
        0x10, 0x11, 0x12,          # SHIFT / CTRL / MENU(ALT)
        0xA0, 0xA1,                # LSHIFT / RSHIFT
        0xA2, 0xA3,                # LCTRL / RCTRL
        0xA4, 0xA5,                # LMENU / RMENU
    })

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._hook_handle = None
        self._hook_proc_ref = None
        self._win_down = False
        self._win_compensated = False       # 本次 Win 是否已补发给系统（Win+其它组合场景）
        self._win_used_for_winv = False     # 本次 Win 是否已用于 Win+V（全程吞掉，不补发）
        self._v_held = False                # V 当前是否处于按住状态（长按自动重复 keydown 去重）
        self._v_up_to_swallow = 0           # 待吞掉的 V keyup 计数（与吞掉的 V down 配对）
        self._win_press_time = 0.0          # 物理 Win 按下时间戳（看门狗用）
        self._win_timeout = 1.5             # 超过该秒数仍为 win_down → 判定 up 事件丢失，兜底复位
        self._last_trigger_time = 0.0
        # 超时看门狗：兜底「物理 Win 抬起事件丢失」导致的 Win 键卡住
        self._watchdog = QtCore.QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._watchdog_tick)

    def start(self) -> bool:
        """安装低级键盘钩子（WH_KEYBOARD_LL）并启动看门狗。"""
        if not _on_windows:
            return False
        self._hook_proc_ref = HOOKPROC(self._kb_ll_proc)
        h_mod = _kernel32.GetModuleHandleW(None)
        try:
            self._hook_handle = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._hook_proc_ref, h_mod, 0)
        except Exception:
            self._hook_handle = None
        if self._hook_handle:
            _safe_log(
                f"WH_KEYBOARD_LL 钩子已安装 (Win+V 接管, handle={self._hook_handle})",
                logging.INFO,
            )
        self._watchdog.start()
        return bool(self._hook_handle)

    def stop(self) -> None:
        self._watchdog.stop()
        if self._hook_handle:
            try:
                _user32.UnhookWindowsHookEx(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None

    def _watchdog_tick(self) -> None:
        """看门狗：若 Win 按下超过阈值仍无 up，判定物理 up 事件丢失，兜底补发 up 并复位。"""
        if not self._win_down:
            return
        if time.time() - self._win_press_time <= self._win_timeout:
            return
        # 物理 Win 可能早已抬起但 up 事件丢失
        if self._win_compensated:
            # 曾补发过 Win down：必须补发 up 配对，否则系统认为 Win 一直按住
            self._send_key(self.VK_LWIN, keyup=True)
        self._win_down = False
        self._win_compensated = False
        self._win_used_for_winv = False

    def _send_key(self, vk: int, keyup: bool = False) -> None:
        """把之前吞掉的 Win 键重新注入系统（keybd_event 注入输入带 LLKHF_INJECTED，钩子会放行不递归）。"""
        flags = 0x0002 if keyup else 0  # KEYEVENTF_KEYUP
        try:
            _user32.keybd_event(vk, 0, flags, 0)
        except Exception:
            pass

    def _kb_ll_proc(self, n_code, w_param, l_param):
        if n_code >= 0 and l_param:
            kb = ctypes.cast(
                l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)
            # 我们自己注入补发的按键（LLKHF_INJECTED）直接放行，避免递归
            if int(kb.flags) & self.LLKHF_INJECTED:
                return _user32.CallNextHookEx(
                    self._hook_handle, n_code, w_param, l_param)
            if w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                if vk in (self.VK_LWIN, self.VK_RWIN):
                    # 先吞掉 Win 按下，稍后按实际组合决定是否补发回系统
                    self._win_down = True
                    self._win_compensated = False
                    self._win_used_for_winv = False
                    self._win_press_time = time.time()
                    return 1
                if vk == self.VK_V and self._win_down:
                    # 任何 Win+V（含 Ctrl/Alt+Win+V）都吞掉并标记本次 Win 用于剪贴板相关，
                    # 避免 Win up 时误补发弹开始菜单；仅无 Ctrl/Alt 时触发弹窗
                    self._win_used_for_winv = True
                    if not self._v_held:
                        self._v_held = True
                        # 记录一次待吞的 V keyup：抬起时配对吞掉，
                        # 避免前台程序收到无 down 配对的孤立 V 抬起
                        self._v_up_to_swallow += 1
                    ctrl = bool(
                        _user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
                    alt = bool(
                        _user32.GetAsyncKeyState(self.VK_MENU) & 0x8000)
                    if not ctrl and not alt:
                        now = time.time()
                        if now - self._last_trigger_time > 0.3:
                            self._last_trigger_time = now
                            _safe_log("Win+V 已触发（剪贴板历史）", logging.INFO)
                            # 关键：WH_KEYBOARD_LL 回调必须在 LowLevelHooksTimeout 内返回，
                            # 否则 Windows 直接放行本次 V 按下（前台程序收到裸 V）。
                            # 弹窗创建/布局等重活延迟到事件循环执行，确保吞键生效。
                            QtCore.QTimer.singleShot(0, self.triggered.emit)
                    return 1  # 吞掉 V，阻止系统剪贴板历史
                if vk == self.VK_V:
                    # 裸 V 按下（未按住 Win）：清除残留吞键状态，避免误吞本次 V 的 keyup
                    self._v_held = False
                    self._v_up_to_swallow = 0
                if self._win_down and vk in self._MODIFIER_VKS:
                    # Win 按住期间的修饰键（Ctrl/Alt/Shift）：放行，不触发补发
                    return _user32.CallNextHookEx(
                        self._hook_handle, n_code, w_param, l_param)
                if self._win_down:
                    # Win + 其它键（如 E/D/R）：补发 Win 按下，让系统执行 Win+组合
                    if not self._win_compensated:
                        self._send_key(self.VK_LWIN)
                        self._win_compensated = True
                    # 放行当前键，系统即可识别 Win+该键
            elif w_param in (self.WM_KEYUP, self.WM_SYSKEYUP):
                if vk == self.VK_V:
                    self._v_held = False
                    if self._v_up_to_swallow > 0:
                        # 与之前吞掉的 V keydown 配对，避免前台程序收到孤立 V keyup
                        self._v_up_to_swallow -= 1
                        return 1
                if vk in (self.VK_LWIN, self.VK_RWIN):
                    if self._win_compensated:
                        # 已补发过 Win down（Win+其它组合）：必须补发 up 配对，
                        # 否则系统认为 Win 一直被按住 → 后续按键全变成 Win+组合（按键混乱）
                        self._send_key(self.VK_LWIN, keyup=True)
                    elif not self._win_used_for_winv:
                        # Win 单独键：补发一次短按，让系统弹开始菜单
                        self._send_key(self.VK_LWIN)
                        self._send_key(self.VK_LWIN, keyup=True)
                    # else: Win+V 全程吞，未补发过 down，无需补 up
                    self._win_down = False
                    self._win_compensated = False
                    self._win_used_for_winv = False
                    return 1  # 吞掉原始 Win 抬起
        return _user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)


class FolderFavoritesHotkeyService(QtCore.QObject):
    """管理 GlobalMouseMonitor + Win+V 键盘钩子，供 l_notepad 在任意程序中唤起。"""

    started = QtCore.Signal(bool)
    failed = QtCore.Signal(str)
    # Win+V：接管系统剪贴板历史，弹出 l_notepad 自己的「剪贴板历史」小窗口
    win_v_triggered = QtCore.Signal()

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._monitor: GlobalMouseMonitor | None = None
        self._kb_hotkey: GlobalKeyboardWinVHotkey | None = None

    def is_available(self) -> bool:
        return _on_windows

    def start(self, on_triggered: Callable[[], None]) -> bool:
        if not _on_windows:
            self.failed.emit("Ctrl+鼠标 快捷键仅支持 Windows")
            self.started.emit(False)
            return False

        button = load_hotkey_button()
        self._monitor = GlobalMouseMonitor(trigger_button=button)
        self._monitor.triggered.connect(on_triggered)
        ok = bool(self._monitor.start())
        self.started.emit(ok)
        if not ok:
            self.failed.emit("全局鼠标钩子启动失败")

        # Win+V 键盘钩子（接管系统剪贴板历史）
        try:
            self._kb_hotkey = GlobalKeyboardWinVHotkey(self)
            self._kb_hotkey.triggered.connect(self.win_v_triggered)
            if not self._kb_hotkey.start():
                _safe_log("Win+V 键盘钩子启动失败", logging.WARNING)
        except Exception as e:
            _safe_log(f"Win+V 键盘钩子异常: {e}", logging.WARNING)
        return ok

    def stop(self) -> None:
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        if self._kb_hotkey is not None:
            self._kb_hotkey.stop()
            self._kb_hotkey = None

    def set_trigger_button(self, button: str) -> None:
        save_hotkey_button(button)
        if self._monitor is not None:
            self._monitor.set_trigger_button(button)
