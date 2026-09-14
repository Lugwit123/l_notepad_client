# -*- coding: utf-8 -*-
"""l_notepad 内嵌的文件夹收藏全局快捷键（Ctrl + 中键/左键）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6 import QtCore

from . import paths

BUTTON_MSGS = {
    "middle": {"down": 0x0207, "name": "中键", "vk": 0x04},
    "left": {"down": 0x0201, "name": "左键", "vk": 0x01},
}

# 剪贴板历史的鼠标「备用入口」：Shift + 中键。
# 固定用中键、不跟随 hotkey_button 配置 —— 这样「Ctrl+鼠标=收藏面板」无论被设成
# 中键还是左键，都不会和它撞车。存在意义：远控/云桌面下键盘快捷键可能被控制端
# 拦掉或走别的注入通道，而鼠标事件实测是可靠转发的（钩子里旁听，不吞按键）。
CLIPBOARD_BTN_VK = BUTTON_MSGS["middle"]["vk"]        # 0x04 VK_MBUTTON
CLIPBOARD_BTN_DOWN = BUTTON_MSGS["middle"]["down"]    # 0x0207 WM_MBUTTONDOWN
CLIPBOARD_BTN_NAME = BUTTON_MSGS["middle"]["name"]

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
GIT_HTTP_CONNECT_TIMEOUT_SEC = 5
QS_ALLINPUT = 0x04FF
PM_REMOVE = 0x0001
# 用来标记「我们自己 keybd_event 补发」的按键（写在 KBDLLHOOKSTRUCT.dwExtraInfo）。
# 关键：不能用 LLKHF_INJECTED 来区分敌我——远控（网易UU远程/Windows App/mstsc）、
# 自动化脚本的键鼠输入同样带 LLKHF_INJECTED，一刀切跳过会让 Win+V 在远控会话里
# 彻底失效（而鼠标那条路径没这个过滤，所以 Ctrl+中键一直好用）。
INJECT_MAGIC = 0x4C4E4F54          # 'LNOT'


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),   # 读成整数用：区分自己补发的按键（见 INJECT_MAGIC）
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
    # 钩子线程自己的消息泵（GlobalKeyboardWinVHotkey._thread_main）
    _user32.MsgWaitForMultipleObjects.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
    ]
    _user32.MsgWaitForMultipleObjects.restype = ctypes.wintypes.DWORD
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(ctypes.wintypes.MSG),
        ctypes.c_void_p,
        ctypes.wintypes.UINT,
        ctypes.wintypes.UINT,
        ctypes.wintypes.UINT,
    ]
    _user32.PeekMessageW.restype = ctypes.wintypes.BOOL
    # 第 4 参 dwExtraInfo 是 ULONG_PTR：必须声明，否则 64 位下 INJECT_MAGIC 传不对
    _user32.keybd_event.argtypes = [
        ctypes.wintypes.BYTE,
        ctypes.wintypes.BYTE,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.WPARAM,
    ]
else:
    _user32 = None
    _kernel32 = None
    HOOKPROC = None


class GlobalMouseMonitor(QtCore.QObject):
    """全局鼠标监控器：Ctrl+中键/左键 → 收藏面板；Shift+中键 → 剪贴板历史（备用入口）。"""

    triggered = QtCore.Signal()             # Ctrl+鼠标 → 文件夹收藏面板
    clipboard_triggered = QtCore.Signal()   # Shift+中键 → 剪贴板历史弹窗

    VK_LCONTROL = 0xA2
    VK_RCONTROL = 0xA3
    VK_LSHIFT = 0xA0
    VK_RSHIFT = 0xA1

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
        self._last_clip_time = 0.0          # Shift+中键 去抖（与 Ctrl 的分开计）
        self._mbtn_was_down = False         # 中键下降沿跟踪（轮询备用路径用）
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
        # 只在关心的两个「按下」消息上查修饰键状态：鼠标移动事件每秒上百个，
        # 每次多两次 GetAsyncKeyState 是没必要的开销（钩子回调必须够快）。
        if n_code >= 0 and (w_param == self._btn_down_msg
                            or w_param == CLIPBOARD_BTN_DOWN):
            shift_down = self._shift_down()
            # Shift+中键 → 剪贴板历史（备用入口，固定中键）
            if w_param == CLIPBOARD_BTN_DOWN and shift_down:
                self._fire_clipboard(f"Shift+{CLIPBOARD_BTN_NAME} 已触发")
            # Ctrl+鼠标 → 收藏面板；按着 Shift 时让位给上面那条
            elif w_param == self._btn_down_msg:
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

        # Shift+中键 的轮询兜底：按「中键下降沿 + Shift 按住」判断，与钩子同一套语义
        # （钩子若被系统摘掉或漏事件，这条仍能兜住）
        mbtn_down = bool(_user32.GetAsyncKeyState(CLIPBOARD_BTN_VK) & 0x8000)
        if mbtn_down and not self._mbtn_was_down and self._shift_down():
            self._fire_clipboard(
                f"Shift+{CLIPBOARD_BTN_NAME} 已触发（轮询备用）")
        self._mbtn_was_down = mbtn_down

        if now - self._last_heartbeat_time > 60.0:
            self._last_heartbeat_time = now
            _safe_log(
                f"热键监听 heartbeat #{self._poll_count}, "
                f"ctrl={ctrl_down}, hook={'OK' if self._hook_handle else 'FAIL'}"
            )

        self._ctrl_was_down = ctrl_down

    def _shift_down(self) -> bool:
        """Shift 是否物理按住（左右分开查：GetAsyncKeyState 不认 VK_SHIFT 这个合并码）。"""
        return bool(
            (_user32.GetAsyncKeyState(self.VK_LSHIFT) & 0x8000)
            or (_user32.GetAsyncKeyState(self.VK_RSHIFT) & 0x8000)
        )

    def _fire_clipboard(self, label: str) -> None:
        """发剪贴板历史事件（0.2s 去抖：中键连点、钩子与轮询重复都只算一次）。"""
        now = time.time()
        if now - self._last_clip_time <= 0.2:
            return
        self._last_clip_time = now
        _safe_log(label, logging.INFO)
        self.clipboard_triggered.emit()

    def set_trigger_button(self, button: str) -> None:
        button = button if button in BUTTON_MSGS else "middle"
        self._trigger_button = button
        info = BUTTON_MSGS[button]
        self._btn_down_msg = info["down"]
        self._btn_name = info["name"]
        self._btn_vk = info["vk"]
        _safe_log(
            f"触发按钮已切换为: Ctrl+{self._btn_name}；"
            f"剪贴板历史备用入口: Shift+{CLIPBOARD_BTN_NAME}",
            logging.INFO,
        )


class GlobalKeyboardWinVHotkey(QtCore.QObject):
    """低级键盘钩子：接管 Win+V（阻止系统剪贴板历史 / 开始菜单）。

    策略：Win 键按下即吞掉（系统收不到 Win，不弹开始菜单），
    Win 按住期间按下 V 则触发剪贴板历史回调并吞掉 V。
    线程：钩子跑在**独立线程**（见 _thread_main），该线程只做消息泵 + 看门狗；
    回调里只吞键并 emit 信号（跨线程自动排队到主线程），弹窗等重活全在主线程做——
    回调一旦超过 LowLevelHooksTimeout，Windows 会静默摘掉整个钩子，热键就再也不响应。
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
        self._injected_noted = False        # 首次遇到「别人注入」的按键时记一条日志（只记一次）
        self._win_press_time = 0.0          # 物理 Win 按下时间戳（看门狗用）
        # 超过该秒数仍为 win_down → 判定 Win 抬起事件丢失，兜底复位。
        # 为什么必须给得足够大：Win 按下被本钩子吞掉后系统收不到 Win（开始菜单不弹），
        # 用户「按住 Win 想一下再按 V」很常见；而实测（吞掉 keydown 后立刻以及 300ms 后
        # 查 GetAsyncKeyState，高位都是 0）**拿不到被吞掉的 Win 的物理状态**，
        # 超时是唯一可用信号。之前 1.5s 会在正常手势中途提前清掉 _win_down，
        # 导致紧随其后的 V 被当成「裸 V」放给前台程序 → Win+V 静默失效（还多打出一个 v）。
        self._win_timeout = 8.0
        self._last_trigger_time = 0.0
        # 钩子装在独立线程上（见 _thread_main）：LL 钩子回调只派发给安装它的线程，
        # 且必须在 LowLevelHooksTimeout 内返回，否则 Windows 会**静默摘掉整个钩子**。
        # 装在 Qt 主线程时，主线程任何一次 >300ms 的卡顿（HTTP/扫盘/重布局）都会把热键
        # 打死且无法自愈；独立线程只跑消息泵 + 看门狗，不受 UI 影响。
        # 看门狗也从 QTimer 挪进该线程（状态只被它碰，无需加锁）。
        self._thread: threading.Thread | None = None
        self._stop_flag = False
        self._installed = threading.Event()
        self._install_ok = False
        self._pump_timeout_ms = 500         # 消息泵阻塞超时 = 看门狗检查间隔

    def start(self) -> bool:
        """在独立线程里安装低级键盘钩子（WH_KEYBOARD_LL），并起消息泵 + 看门狗。"""
        if not _on_windows:
            return False
        if self._thread is not None and self._thread.is_alive():
            return self._install_ok
        self._stop_flag = False
        self._install_ok = False
        self._installed.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="WinVHotkeyHook", daemon=True)
        self._thread.start()
        # 等安装结果：失败也要尽快返回 False，别让调用方干等
        self._installed.wait(2.0)
        return self._install_ok

    def stop(self) -> None:
        """停消息泵；钩子由线程自己卸（卸钩子应由安装它的线程执行）。"""
        self._stop_flag = True
        th, self._thread = self._thread, None
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        self._hook_handle = None

    def _thread_main(self) -> None:
        """钩子线程主体：装钩子 → 抽消息（顺带跑看门狗）→ 卸钩子。"""
        try:
            self._hook_proc_ref = HOOKPROC(self._kb_ll_proc)   # 必须保活
            h_mod = _kernel32.GetModuleHandleW(None)
            self._hook_handle = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._hook_proc_ref, h_mod, 0)
        except Exception as exc:
            self._hook_handle = None
            _safe_log(f"SetWindowsHookExW 异常: {exc}", logging.WARNING)
        self._install_ok = bool(self._hook_handle)
        if self._install_ok:
            _safe_log(
                f"WH_KEYBOARD_LL 钩子已安装 (Win+V 接管, 独立线程, "
                f"handle={self._hook_handle})",
                logging.INFO,
            )
        else:
            _safe_log("WH_KEYBOARD_LL 钩子安装失败，Win+V 热键不可用", logging.WARNING)
        self._installed.set()          # 先放行调用方，再进消息泵
        if not self._install_ok:
            return

        msg = ctypes.wintypes.MSG()
        try:
            while not self._stop_flag:
                # 阻塞等消息（不占 CPU）；超时点正好当看门狗检查点
                _user32.MsgWaitForMultipleObjects(
                    0, None, False, self._pump_timeout_ms, QS_ALLINPUT)
                while _user32.PeekMessageW(
                        ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                self._watchdog_tick()
        except Exception as exc:
            _safe_log(f"Win+V 钩子线程异常退出: {exc}", logging.ERROR)
        finally:
            if self._hook_handle:
                try:
                    _user32.UnhookWindowsHookEx(self._hook_handle)
                except Exception:
                    pass
                self._hook_handle = None
                _safe_log("WH_KEYBOARD_LL 钩子已卸载", logging.INFO)

    def _watchdog_tick(self) -> None:
        """看门狗：Win 按下超过阈值仍无 up → 判定抬起事件丢失，兜底补发 up 并复位。

        由钩子线程的消息泵每 _pump_timeout_ms 调一次（不再是主线程的 QTimer）——
        状态只被这一个线程碰，所以不需要加锁；也不会因为 UI 卡顿而漏检。
        """
        if not self._win_down:
            return
        if time.time() - self._win_press_time <= self._win_timeout:
            return
        # 走到这里说明 Win 按下后 8 秒内一条相关按键都没收到：要么 up 事件真丢了，
        # 要么钩子已被系统摘掉（回调超 LowLevelHooksTimeout 会被静默卸载）。
        # 留痕，便于下次排查「Win+V 不生效」到底是哪一类。
        _safe_log(
            f"Win 抬起事件疑似丢失（按住超过 {self._win_timeout}s）：兜底复位 Win 状态",
            logging.WARNING,
        )
        # 物理 Win 可能早已抬起但 up 事件丢失
        if self._win_compensated:
            # 曾补发过 Win down：必须补发 up 配对，否则系统认为 Win 一直按住
            self._send_key(self.VK_LWIN, keyup=True)
        self._win_down = False
        self._win_compensated = False
        self._win_used_for_winv = False

    def _send_key(self, vk: int, keyup: bool = False) -> None:
        """把之前吞掉的 Win 键重新注入系统。

        注入的按键带 LLKHF_INJECTED + dwExtraInfo=INJECT_MAGIC，钩子据此认出
        「这是自己补发的」并放行（避免递归）；别人的注入输入（远控/自动化）没有这个
        标记，照常走 Win+V 逻辑。
        """
        flags = 0x0002 if keyup else 0  # KEYEVENTF_KEYUP
        try:
            _user32.keybd_event(vk, 0, flags, INJECT_MAGIC)
        except Exception:
            pass

    def _kb_ll_proc(self, n_code, w_param, l_param):
        if n_code >= 0 and l_param:
            kb = ctypes.cast(
                l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)
            # 只放行「我们自己补发」的按键（带 INJECT_MAGIC），不能一刀切跳过所有注入键：
            # 远控/自动化的键鼠输入同样带 LLKHF_INJECTED，全跳过就等于 Win+V 在远控会话里
            # 永不生效（鼠标那条路径没有这个过滤，所以 Ctrl+中键一直好用——症状即由此而来）。
            if int(kb.flags) & self.LLKHF_INJECTED:
                if int(kb.dwExtraInfo or 0) == INJECT_MAGIC:
                    return _user32.CallNextHookEx(
                        self._hook_handle, n_code, w_param, l_param)
                if not self._injected_noted:
                    self._injected_noted = True
                    _safe_log(
                        "承接注入输入（远控/自动化）：Win+V 逻辑对注入键同样生效",
                        logging.INFO,
                    )
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
                    fresh_press = not self._v_held
                    if fresh_press:
                        self._v_held = True
                        # 记录一次待吞的 V keyup：抬起时配对吞掉，
                        # 避免前台程序收到无 down 配对的孤立 V 抬起
                        self._v_up_to_swallow += 1
                    ctrl = bool(
                        _user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
                    alt = bool(
                        _user32.GetAsyncKeyState(self.VK_MENU) & 0x8000)
                    # 只在「本次是新按下」时触发：长按 V 的自动重复由 fresh_press 挡掉。
                    # 刻意不再叠一层 0.3s 时间防抖——V 已经被吞掉了，「只吞不触发」就是
                    # 纯静默失效（用户按快一点会以为热键坏了）；而重复触发只是让弹窗
                    # 重新摆一次位置，代价远小于「按了没反应」。
                    if fresh_press and not ctrl and not alt:
                        self._last_trigger_time = time.time()
                        _safe_log("Win+V 已触发（剪贴板历史）", logging.INFO)
                        # 回调必须在 LowLevelHooksTimeout 内返回，否则 Windows 直接放行
                        # 本次 V 按下（前台程序收到裸 V）。emit 只是往主线程投一个事件
                        # （接收者在主线程 → 自动排队连接），弹窗创建/布局都在主线程做，
                        # 这里不阻塞；也不再依赖「回调恰好跑在主线程」这个前提。
                        self.triggered.emit()
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
    # Shift+中键：剪贴板历史的鼠标备用入口（与 Win+V 弹同一个窗口）
    clipboard_triggered = QtCore.Signal()

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
        # Shift+中键 → 剪贴板历史（备用入口）：转发成服务自己的信号给业务侧接
        self._monitor.clipboard_triggered.connect(self.clipboard_triggered)
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
