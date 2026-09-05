# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import logging
import os

# 统一使用 pytracemp lprint 并强制 logging 模式（标准日志格式：时间-级别-消息，无源码跟踪）。
# 注意：环境里 Lugwit_Debug 可能已被预置为 'true'（inspect），这里必须强制覆盖。
os.environ["Lugwit_Debug"] = "logging"

import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets
from l_qframelesswindow import L_FramelessMainWindow

from . import auth as authmod
from . import file_store
from . import server_config
from .api_client import ApiError, LogDto, NoteDto, NotepadApi
from .folder_favorites_hotkey import FolderFavoritesHotkeyService
from .paths import data_root
from pytracemp import lprint
from .settings_widget import SettingsWidget
from .ui import MainWindow as NotepadContentWindow


DOUBLE_CTRL_MIN_GAP_SEC = 0.05
DOUBLE_CTRL_MAX_GAP_SEC = 0.15

_TOKEN_FILE = data_root() / "auth_token.json"


class SettingsDialog(QtWidgets.QDialog):
    """设置弹窗，包装 SettingsWidget 为独立对话框。"""

    def __init__(self, settings_widget: SettingsWidget, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(520, 560)
        self.setMinimumSize(400, 400)
        self._settings_widget = settings_widget
        
        # 设置 parent，让 SettingsWidget 能访问 MainWindow 的 callback
        self._settings_widget.setParent(self)

        # 布局：复用已有的 SettingsWidget，外面包一层边距
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        # 标题
        title = QtWidgets.QLabel("⚙️ 设置")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #E8EAED; padding: 4px 0;")
        outer.addWidget(title)

        # 分隔线
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setStyleSheet("color: rgba(255, 255, 255, 0.2);")
        outer.addWidget(line)

        # 嵌入 SettingsWidget
        outer.addWidget(self._settings_widget)

        # 关闭按钮
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        outer.addLayout(btn_row)

        # 样式
        self.setStyleSheet("""
            QDialog {
                background: #1a1a24;
            }
            QFrame {
                background: transparent;
            }
            QPushButton {
                background: #3a3a4a;
                color: #E0E0E0;
                border: none;
                border-radius: 4px;
                padding: 6px 16px;
                font-size: 13px;
            }
            QPushButton:hover {
                background: #4a4a5a;
            }
            QPushButton:pressed {
                background: #555566;
            }
        """)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # 关闭时隐藏而非销毁，保留 SettingsWidget 供下次复用
        event.ignore()
        self.hide()


# 可选热键：名称 -> (vk_codes 集合, 显示名)
HOTKEY_OPTIONS: dict[str, tuple[set[int], str]] = {
    "Ctrl":   ({0x11, 0xA2, 0xA3}, "Ctrl"),
    "Alt":    ({0x12, 0xA4, 0xA5}, "Alt"),
    "Shift":  ({0x10, 0xA0, 0xA1}, "Shift"),
}
DEFAULT_HOTKEY_KEY = "Ctrl"
IPC_SERVER_NAME = "l_notepad_pc_ipc"


class TeeStream:
    def __init__(self, original_stream=None, file_handle=None) -> None:
        self._original_stream = original_stream
        self._file_handle = file_handle

    def write(self, text: str) -> int:
        if self._original_stream is not None:
            try:
                self._original_stream.write(text)
                self._original_stream.flush()
            except Exception:
                pass
        if self._file_handle is not None:
            try:
                self._file_handle.write(text)
                self._file_handle.flush()
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        if self._original_stream is not None:
            try:
                self._original_stream.flush()
            except Exception:
                pass
        if self._file_handle is not None:
            try:
                self._file_handle.flush()
            except Exception:
                pass


class FileLogTailer(QtCore.QObject):
    def __init__(self, log_path: Path, ui_writer, parent=None) -> None:
        super().__init__(parent)
        self._log_path = log_path
        self._ui_writer = ui_writer
        self._offset = 0
        self._buffer = ""
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    def poll(self) -> None:
        if not self._log_path.exists():
            return
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except Exception:
            return
        if not chunk:
            return
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                try:
                    self._ui_writer(line)
                except Exception:
                    pass


class QtLogHandler(logging.Handler):
    def __init__(self, ui_writer) -> None:
        super().__init__()
        self._ui_writer = ui_writer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._ui_writer(msg, level=record.levelname)
        except Exception:
            pass


class SafeFileHandler(logging.FileHandler):
    """FileHandler that never lets logging teardown/flush errors leak into Qt callbacks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        except (OSError, ValueError):
            pass

    def flush(self) -> None:
        try:
            super().flush()
        except (OSError, ValueError):
            pass


def _is_stdout_handler(handler: logging.Handler) -> bool:
    return isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)


def _remove_unsafe_stream_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if _is_stdout_handler(handler):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


class DoubleKeyWatcher:
    """Listary-style 全局双击按键监听，支持可配置热键。"""

    def __init__(
        self,
        callback,
        log_callback=None,
        min_gap_sec: float = DOUBLE_CTRL_MIN_GAP_SEC,
        max_gap_sec: float = DOUBLE_CTRL_MAX_GAP_SEC,
        hotkey_key: str = DEFAULT_HOTKEY_KEY,
    ) -> None:
        self._callback = callback
        self._log_callback = log_callback
        self._min_gap_sec = float(min_gap_sec)
        self._max_gap_sec = float(max_gap_sec)
        self._hotkey_key = hotkey_key
        self._key_codes = HOTKEY_OPTIONS.get(hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[0]
        self._last_triggered_at = 0.0
        self._lock = threading.Lock()
        self._pending_logs: list[tuple[str, int]] = []
        self._trigger_pending = False
        self._key_down = False
        self._last_key_press_at = 0.0
        self._last_miss_logged_at = 0.0
        self._miss_log_count = 0
        self._hook_id = None
        self._hook_proc = None
        self._hook_thread_id = 0
        self._flush_timer = QtCore.QTimer()
        self._flush_timer.setInterval(15)
        self._flush_timer.timeout.connect(self._flush_pending)
        self._flush_timer.start()
        if sys.platform == "win32":
            key_name = HOTKEY_OPTIONS.get(hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[1]
            self._log(
                f"启动低级键盘 Hook 双击 {key_name} 监听，"
                f"有效间隔 {self._min_gap_sec:.2f}-{self._max_gap_sec:.2f}s"
            )
            self._hook_thread = threading.Thread(target=self._run_keyboard_hook, daemon=True)
            self._hook_thread.start()
        else:
            self._log("当前平台暂不支持全局双击热键监听")

    def _log(self, message: str, level: int = logging.INFO) -> None:
        with self._lock:
            self._pending_logs.append((message, level))

    def _log_miss(self, message: str, now: float) -> None:
        self._miss_log_count += 1
        if now - self._last_miss_logged_at < 2.0 and self._miss_log_count % 20 != 0:
            return
        self._last_miss_logged_at = now
        self._log(message, logging.DEBUG)

    def _on_key_pressed(self) -> None:
        now = time.monotonic()
        gap = now - self._last_key_press_at
        key_name = HOTKEY_OPTIONS.get(self._hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[1]
        in_range = self._min_gap_sec <= gap <= self._max_gap_sec
        message = (
            f"{key_name} 按下，间隔 {gap:.3f}s"
            f"（有效范围 {self._min_gap_sec:.2f}~{self._max_gap_sec:.2f}s"
            f"{' 命中' if in_range else ' 未命中'}）"
        )
        if in_range:
            self._log(message)
            self._trigger()
            self._last_key_press_at = 0.0
            self._miss_log_count = 0
        else:
            self._log_miss(message, now)
            self._last_key_press_at = now

    def update_interval(self, max_gap_sec: float) -> None:
        try:
            value = max(self._min_gap_sec, min(1.0, float(max_gap_sec)))
        except Exception:
            return
        self._max_gap_sec = value
        key_name = HOTKEY_OPTIONS.get(self._hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[1]
        self._log(
            f"双击 {key_name} 间隔已更新，"
            f"有效间隔 {self._min_gap_sec:.2f}-{self._max_gap_sec:.2f}s"
        )

    def update_key(self, hotkey_key: str) -> None:
        """更新监听的热键。"""
        if hotkey_key not in HOTKEY_OPTIONS:
            return
        self._hotkey_key = hotkey_key
        self._key_codes = HOTKEY_OPTIONS[hotkey_key][0]
        self._key_down = False
        self._last_key_press_at = 0.0
        key_name = HOTKEY_OPTIONS[hotkey_key][1]
        self._log(f"热键已切换为: 双击 {key_name}")

    def _run_keyboard_hook(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            wh_keyboard_ll = 13
            wm_keydown = 0x0100
            wm_keyup = 0x0101
            wm_syskeydown = 0x0104
            wm_syskeyup = 0x0105
            wm_quit = 0x0012
            hc_action = 0

            low_level_keyboard_proc = ctypes.WINFUNCTYPE(
                wintypes.LPARAM,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                low_level_keyboard_proc,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.CallNextHookEx.restype = wintypes.LPARAM
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [
                    ("vkCode", wintypes.DWORD),
                    ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            def _proc(n_code, w_param, l_param):
                if n_code == hc_action:
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = int(info.vkCode)
                    msg = int(w_param)
                    if vk in self._key_codes and msg in (wm_keydown, wm_syskeydown):
                        if not self._key_down:
                            self._key_down = True
                            self._on_key_pressed()
                    elif vk in self._key_codes and msg in (wm_keyup, wm_syskeyup):
                        self._key_down = False
                return user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)

            self._hook_proc = low_level_keyboard_proc(_proc)
            self._hook_thread_id = kernel32.GetCurrentThreadId()
            self._hook_id = user32.SetWindowsHookExW(
                wh_keyboard_ll,
                self._hook_proc,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not self._hook_id:
                self._log("低级键盘 Hook 注册失败")
                return
            key_name = HOTKEY_OPTIONS.get(self._hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[1]
            self._log(f"低级键盘 Hook 注册成功，等待双击 {key_name}")

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                if msg.message == wm_quit:
                    break
        except Exception as exc:
            self._log(f"低级键盘 Hook 异常: {exc}")

    def _trigger(self) -> None:
        now = time.monotonic()
        if now - self._last_triggered_at < 0.35:
            self._log("快捷键触发过快，已防抖忽略")
            return
        self._last_triggered_at = now
        key_name = HOTKEY_OPTIONS.get(self._hotkey_key, HOTKEY_OPTIONS[DEFAULT_HOTKEY_KEY])[1]
        self._log(f"双击 {key_name} 已触发")
        with self._lock:
            self._trigger_pending = True

    def _flush_pending(self) -> None:
        with self._lock:
            logs = self._pending_logs[:]
            self._pending_logs.clear()
            trigger_pending = self._trigger_pending
            self._trigger_pending = False
        if self._log_callback is not None:
            for message, level in logs:
                try:
                    self._log_callback(message, level)
                except TypeError:
                    try:
                        self._log_callback(message)
                    except Exception:
                        pass
                except Exception:
                    pass
        if trigger_pending:
            try:
                self._callback()
            except Exception:
                self._log("快捷键回调执行失败")

    def release(self) -> None:
        self._flush_timer.stop()
        if sys.platform == "win32" and self._hook_id:
            try:
                import ctypes

                ctypes.windll.user32.UnhookWindowsHookEx(self._hook_id)
                if self._hook_thread_id:
                    ctypes.windll.user32.PostThreadMessageW(self._hook_thread_id, 0x0012, 0, 0)
                self._log("低级键盘 Hook 已释放")
            except Exception:
                self._log("低级键盘 Hook 释放失败")
            self._hook_id = None


def _setup_tray_icon(
    app: QtWidgets.QApplication,
    win: QtWidgets.QMainWindow,
    icon: QtGui.QIcon,
) -> QtWidgets.QSystemTrayIcon | None:
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        return None

    tray = QtWidgets.QSystemTrayIcon(icon, app)
    tray.setToolTip("L Notepad")

    menu = QtWidgets.QMenu()
    act_show = menu.addAction("显示/恢复")
    act_hide = menu.addAction("最小化到托盘")
    menu.addSeparator()
    act_restart = menu.addAction("重启")
    act_quit = menu.addAction("退出")

    def _show_window() -> None:
        win.show_from_hotkey()

    def _hide_window() -> None:
        win.showMinimized()

    def _restart_app() -> None:
        content = getattr(win, "content_window", None)
        target = content if content is not None else win
        if hasattr(target, "_restart_app"):
            target._restart_app()

    def _quit_app() -> None:
        # ui.py closes-to-tray by default; force real close before quitting.
        setattr(win, "_allow_close", True)
        win.close()
        app.quit()
        # 强制退出，确保 app.quit() 起作用
        QtCore.QTimer.singleShot(100, lambda: sys.exit(0))

    act_show.triggered.connect(_show_window)
    act_hide.triggered.connect(_hide_window)
    act_restart.triggered.connect(_restart_app)
    act_quit.triggered.connect(_quit_app)

    if hasattr(win, "set_tray_icon"):
        win.set_tray_icon(tray)
    tray.setContextMenu(menu)

    def _on_activated(reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            _show_window()

    tray.activated.connect(_on_activated)
    tray.show()
    return tray


def _set_windows_appid(appid: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def _parse_open_file_args(argv: list[str]) -> list[str]:
    files: list[str] = []
    for arg in argv[1:]:
        if not arg:
            continue
        if arg.startswith("-"):
            continue
        files.append(str(Path(arg)))
    return files


def _send_ipc_open_files(files: list[str]) -> bool:
    if not files:
        return False
    socket = QtNetwork.QLocalSocket()
    socket.connectToServer(IPC_SERVER_NAME)
    if not socket.waitForConnected(1000):
        return False
    payload = json.dumps({"files": files}, ensure_ascii=False).encode("utf-8")
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(1000)
    socket.disconnectFromServer()
    return True


@dataclass(frozen=True)
class _IndexedNote:
    note_id: int
    rel_path: str
    dto: NoteDto


def _stable_note_id(rel_path: str) -> int:
    """Stable int id from relative path (survives mtime sort changes)."""
    raw = zlib.crc32(rel_path.encode("utf-8")) & 0x7FFFFFFF
    return raw if raw != 0 else 1


class LocalNotepadApi:
    """Provide the same interface as NotepadApi, backed by local files."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = root_dir or file_store.default_root_dir()
        file_store.ensure_root(self.root_dir)
        self._index: list[_IndexedNote] | None = None
        self._by_id: dict[int, _IndexedNote] | None = None

    def list_notes(self) -> list[NoteDto]:
        return [x.dto for x in self._indexed_notes()]

    def get_note(self, note_id: int) -> NoteDto:
        indexed = self._find_by_id(note_id)
        if indexed is None:
            raise ApiError(f"note not found: {note_id}")
        note = file_store.get_note(self.root_dir, indexed.rel_path)
        if note is None:
            self._invalidate_index()
            raise ApiError(f"note not found: {note_id}")
        return self._to_dto(indexed.note_id, note)

    def create_note(self, title: str, content: str) -> NoteDto:
        try:
            created = file_store.create_note(self.root_dir, title=title, content=content)
        except Exception as exc:
            raise ApiError(f"create note failed: {exc}") from exc
        self._invalidate_index()
        note_id = _stable_note_id(created.path)
        return self._to_dto(note_id, created)

    def update_note(self, note_id: int, title: str, content: str) -> NoteDto:
        indexed = self._find_by_id(note_id)
        if indexed is None:
            raise ApiError(f"note not found: {note_id}")
        try:
            updated = file_store.update_note(
                self.root_dir,
                indexed.rel_path,
                new_title=title,
                new_content=content,
            )
        except Exception as exc:
            raise ApiError(f"update note failed: {exc}") from exc
        if updated is None:
            raise ApiError(f"note not found: {note_id}")
        self._invalidate_index()
        new_id = _stable_note_id(updated.path)
        return self._to_dto(new_id, updated)

    def move_note(self, note_id: int, dst_dir: str) -> NoteDto:
        indexed = self._find_by_id(note_id)
        if indexed is None:
            raise ApiError(f"note not found: {note_id}")
        try:
            moved = file_store.move_note(self.root_dir, indexed.rel_path, dst_dir)
        except Exception as exc:
            raise ApiError(f"move note failed: {exc}") from exc
        if moved is None:
            raise ApiError(f"note not found: {note_id}")
        self._invalidate_index()
        new_id = _stable_note_id(moved.path)
        return self._to_dto(new_id, moved)

    def delete_note(self, note_id: int) -> None:
        indexed = self._find_by_id(note_id)
        if indexed is None:
            raise ApiError(f"note not found: {note_id}")
        try:
            ok = file_store.delete_note(self.root_dir, indexed.rel_path)
        except Exception as exc:
            raise ApiError(f"delete note failed: {exc}") from exc
        if not ok:
            raise ApiError(f"note not found: {note_id}")
        self._invalidate_index()

    def _invalidate_index(self) -> None:
        self._index = None
        self._by_id = None

    def _find_by_id(self, note_id: int) -> _IndexedNote | None:
        self._ensure_index()
        assert self._by_id is not None
        return self._by_id.get(int(note_id))

    def _ensure_index(self) -> None:
        if self._index is not None and self._by_id is not None:
            return
        metas = file_store.list_notes_meta(self.root_dir, limit=10_000)
        index: list[_IndexedNote] = []
        by_id: dict[int, _IndexedNote] = {}
        for meta in metas:
            note_id = _stable_note_id(meta.path)
            dto = self._meta_to_dto(note_id, meta)
            item = _IndexedNote(note_id=note_id, rel_path=meta.path, dto=dto)
            index.append(item)
            by_id[note_id] = item
        self._index = index
        self._by_id = by_id

    def _indexed_notes(self) -> list[_IndexedNote]:
        self._ensure_index()
        assert self._index is not None
        return self._index

    @staticmethod
    def _meta_to_dto(note_id: int, meta: file_store.FileNoteMeta) -> NoteDto:
        return NoteDto(
            id=int(note_id),
            title=meta.title,
            content="",
            created_at=meta.created_at,
            updated_at=meta.updated_at,
        )

    @staticmethod
    def _to_dto(note_id: int, note: file_store.FileNote) -> NoteDto:
        return NoteDto(
            id=int(note_id),
            title=note.title,
            content=note.content,
            created_at=note.created_at,
            updated_at=note.updated_at,
        )

    # ---- Server Log API (local filesystem / remote server) ----

    @staticmethod
    def _log_server_url() -> str:
        """Return remote log server URL (persisted config, env, default)."""
        return server_config.log_server_url()

    @staticmethod
    def _log_dir() -> Path:
        import os
        return Path(os.environ.get("L_NOTEPAD_LOG_DIR", r"D:\Temp\Log"))

    def list_logs(self, max_size: int = 2 * 1024 * 1024) -> list[LogDto]:
        """List server log files from remote server, fallback to local on failure."""
        remote_url = self._log_server_url()
        try:
            return self._list_logs_remote(remote_url)
        except Exception as e:
            # 远程获取失败时降级到本地
            lprint(f"[l_notepad] WARNING: Remote log list failed ({e}), falling back to local")
            return self._list_logs_local(max_size)

    def _list_logs_remote(self, base_url: str) -> list[LogDto]:
        """Fetch log list from remote server via HTTP."""
        import json
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(f"{base_url}/api/logs")
            with urllib.request.urlopen(req, timeout=3) as resp:  # 减少超时时间到 3 秒
                data = json.loads(resp.read().decode("utf-8"))
            return [LogDto(**x) for x in (data or [])]
        except Exception as e:
            lprint(f"[l_notepad] ERROR: Remote log list failed: {e}")
            raise ApiError(f"Remote log list failed: {e}") from e

    def _list_logs_local(self, max_size: int = 2 * 1024 * 1024) -> list[LogDto]:
        """List server log files from local filesystem."""
        from datetime import datetime as _dt
        log_root = self._log_dir()
        if not log_root.exists() or not log_root.is_dir():
            return []
        result: list[LogDto] = []
        for p in sorted(log_root.rglob("*")):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            try:
                st = p.stat()
                if st.st_size > max_size:
                    continue
            except OSError:
                continue
            rel = p.relative_to(log_root).as_posix()
            result.append(LogDto(
                path=rel,
                size=st.st_size,
                mtime=_dt.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            ))
        return result

    def get_log(self, log_path: str) -> dict[str, str]:
        """Get server log content from remote server."""
        remote_url = self._log_server_url()
        return self._get_log_remote(remote_url, log_path)

    def _get_log_remote(self, base_url: str, log_path: str) -> dict[str, str]:
        """Fetch log content from remote server via HTTP."""
        import json
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(f"{base_url}/api/logs/{log_path}")
            with urllib.request.urlopen(req, timeout=5) as resp:  # 获取日志内容，超时 5 秒
                data = json.loads(resp.read().decode("utf-8"))
            return data
        except Exception as e:
            lprint(f"[l_notepad] ERROR: Remote get log failed: {e}")
            raise ApiError(f"Remote get log failed: {e}") from e

    def _get_log_local(self, log_path: str) -> dict[str, str]:
        """Get server log content from local filesystem."""
        import os
        log_root = self._log_dir()
        target = (log_root / log_path.replace("/", os.sep)).resolve()
        if log_root.resolve() not in target.parents and target != log_root.resolve():
            raise ApiError(f"Access denied: {log_path}")
        if not target.exists() or not target.is_file():
            raise ApiError(f"Log file not found: {log_path}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"path": log_path, "content": content}

    def update_log(self, log_path: str, content: str) -> None:
        """Update (overwrite) a server log file via remote or local."""
        remote_url = self._log_server_url()
        if remote_url:
            self._update_log_remote(remote_url, log_path, content)
        else:
            self._update_log_local(log_path, content)

    def _update_log_remote(self, base_url: str, log_path: str, content: str) -> None:
        """Update log on remote server via HTTP PUT."""
        import json
        import urllib.request
        import urllib.error
        try:
            payload = json.dumps({"title": "", "content": content}).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/logs/{log_path}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # 更新日志，超时 5 秒
                pass
        except Exception as e:
            lprint(f"[l_notepad] ERROR: Remote update log failed: {e}")
            raise ApiError(f"Remote update log failed: {e}") from e

    def _update_log_local(self, log_path: str, content: str) -> None:
        """Update log on local filesystem."""
        import os
        log_root = self._log_dir()
        target = (log_root / log_path.replace("/", os.sep)).resolve()
        if log_root.resolve() not in target.parents and target != log_root.resolve():
            raise ApiError(f"Access denied: {log_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def delete_log(self, log_path: str) -> None:
        """Delete a server log file via remote, fallback to local."""
        remote_url = self._log_server_url()
        try:
            self._delete_log_remote(remote_url, log_path)
        except Exception as e:
            lprint(f"[l_notepad] WARNING: Remote delete log failed ({e}), falling back to local")
            self._delete_log_local(log_path)

    def _delete_log_remote(self, base_url: str, log_path: str) -> None:
        """Delete log on remote server via HTTP DELETE."""
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                f"{base_url}/api/logs/{log_path}",
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception as e:
            lprint(f"[l_notepad] ERROR: Remote delete log failed: {e}")
            raise ApiError(f"Remote delete log failed: {e}") from e

    def _delete_log_local(self, log_path: str) -> None:
        """Delete log on local filesystem."""
        import os
        log_root = self._log_dir()
        target = (log_root / log_path.replace("/", os.sep)).resolve()
        if log_root.resolve() not in target.parents and target != log_root.resolve():
            raise ApiError(f"Access denied: {log_path}")
        if target.exists() and target.is_file():
            target.unlink()


class LoginDialog(QtWidgets.QDialog):
    """登录对话框：用户名 + 密码，调 Auth Service 登录。

    「记住账号密码」复选框：勾选状态下启动时自动回填账号 + 密码；
    登录成功时按勾选状态决定是否把账号/明文密码写入
    <data_dir>/remembered_login.json；取消勾选后下次登录即清除该文件。
    """

    _REMEMBER_FILE = data_root() / "remembered_login.json"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录 Lugwit 账号")
        self.setFixedWidth(340)
        self._token: str | None = None
        self._username: str = ""

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        title = QtWidgets.QLabel("🔐 登录")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #E8EAED;")
        outer.addWidget(title)

        self._error_label = QtWidgets.QLabel("")
        self._error_label.setStyleSheet("color: #f28b82; font-size: 12px;")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        outer.addWidget(self._error_label)

        form = QtWidgets.QFormLayout()
        self._user_edit = QtWidgets.QLineEdit()
        self._user_edit.setPlaceholderText("用户名（如 admin01）")
        self._pwd_edit = QtWidgets.QLineEdit()
        self._pwd_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._pwd_edit.setPlaceholderText("密码")
        self._pwd_edit.returnPressed.connect(self._on_confirm)
        form.addRow("用户名", self._user_edit)
        form.addRow("密码", self._pwd_edit)
        outer.addLayout(form)

        self._remember_cb = QtWidgets.QCheckBox("记住账号密码")
        self._remember_cb.toggled.connect(self._on_remember_toggled)
        outer.addWidget(self._remember_cb)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QtWidgets.QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QtWidgets.QPushButton("登录")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        outer.addLayout(btn_row)

        # 启动时回填：只有上次勾选了「记住」才会写入文件并回填
        remembered = self._load_remembered()
        if remembered:
            self._user_edit.setText(remembered.get("username", ""))
            self._pwd_edit.setText(remembered.get("password", ""))
            self._remember_cb.setChecked(True)

    def _on_confirm(self) -> None:
        username = self._user_edit.text().strip()
        password = self._pwd_edit.text()
        if not username or not password:
            self._show_error("请输入用户名和密码")
            return
        try:
            body = json.dumps({"username": username, "password": password}).encode("utf-8")
            req = urllib.request.Request(
                f"{server_config.auth_url()}{server_config.auth_route()}/login",
                data=body,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:  # noqa: BLE001
                detail = ""
            self._show_error(str(detail) or f"登录失败 (HTTP {exc.code})")
            return
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"无法连接认证服务: {exc}")
            return
        token = data.get("access_token") or data.get("token")
        if not token:
            self._show_error("认证服务返回异常，缺少 token")
            return
        self._token = token
        self._username = (data.get("user") or {}).get("username", username)
        # 按勾选状态决定是否把账号+密码写入 remembered_login.json
        if self._remember_cb.isChecked():
            self._save_remembered(username, password)
        else:
            self._clear_remembered()
        self.accept()

    def _show_error(self, text: str) -> None:
        self._error_label.setText(text)
        self._error_label.setVisible(True)

    # ── 「记住账号密码」持久化 ──────────────────────────────
    def _on_remember_toggled(self, checked: bool) -> None:
        """取消勾选即时清空持久化文件（避免下次误回填）。"""
        if not checked:
            self._clear_remembered()

    @classmethod
    def _remember_file(cls) -> Path:
        return cls._REMEMBER_FILE

    @staticmethod
    def _save_remembered(username: str, password: str) -> None:
        """把账号 + 密码明文写入 remembered_login.json（用户主动勾选才会触发）。"""
        try:
            f = LoginDialog._REMEMBER_FILE
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(
                json.dumps(
                    {"username": username, "password": password},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 保存「记住账号密码」失败: {exc}")

    @staticmethod
    def _load_remembered() -> dict[str, str] | None:
        """读取上次记住的账号密码；文件不存在或损坏返回 None。"""
        f = LoginDialog._REMEMBER_FILE
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return data
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 读取「记住账号密码」失败: {exc}")
            return None

    @staticmethod
    def _clear_remembered() -> None:
        """删除 remembered_login.json（用户取消勾选或新一次登录未勾选时触发）。"""
        f = LoginDialog._REMEMBER_FILE
        if f.exists():
            try:
                f.unlink()
            except Exception as exc:  # noqa: BLE001
                lprint(f"[l_notepad] 清除「记住账号密码」失败: {exc}")


class MainWindow(L_FramelessMainWindow):
    """本地模式无边框主窗口，使用 L_FramelessMainWindow 作为标题栏外壳。"""

    file_open_requested = QtCore.Signal(str)
    # 后台线程验证 token 通过后，经此信号回到主线程应用登录（QTimer.singleShot 在后台线程无事件循环，回调不会触发）
    _login_restore_signal = QtCore.Signal(str, str)

    def __init__(
        self,
        api: LocalNotepadApi,
        restart_callback=None,
        hotkey_interval_callback=None,
        hotkey_key_callback=None,
    ) -> None:
        super().__init__(vertical_threshold=760)
        self.setWindowTitle("L Notepad")
        self.resize(980, 640)
        self.setMinimumSize(480, 360)
        
        # 设置帮助文档目录
        doc_dir = os.path.join(os.path.dirname(__file__), "doc")
        self.setHelpDocumentDir(doc_dir)

        self.content_window = NotepadContentWindow(
            api,
            restart_callback=restart_callback,
            hotkey_interval_callback=hotkey_interval_callback,
            hotkey_key_callback=hotkey_key_callback,
        )
        if hasattr(self.content_window, "file_open_requested"):
            self.content_window.file_open_requested.connect(self._handle_file_open_request)
        self.content_window.setWindowFlags(QtCore.Qt.WindowType.Widget)
        self.content_window.setParent(self)
        self.content_window.setContentsMargins(0, 0, 0, 0)
        # 不要在嵌入前/外壳显示前提前 show()：会让子部件进入「已显示未曝光」
        # 状态，外壳显示时拿不到首次绘制事件，导致内容区空白。由 setContentWidget
        # 加入布局后随外壳一起显示。
        self.setContentWidget(self.content_window)

        self._sync_title_bar_from_content()
        # 内容窗口请求更新标题栏文本（如 Ctrl+中键识别到的调用程序与路径）
        if hasattr(self.content_window, "title_text_changed"):
            self.content_window.title_text_changed.connect(self.set_title_text)
        
        # 隐藏标题栏左侧图标（l_notepad 不需要显示）
        self.hideTitleBarIcon()
        
        # 设置弹窗引用（延迟创建）
        self._settings_dialog: SettingsDialog | None = None
        
        # 设置标题栏左侧图标的右键菜单（隐藏后仍然有效）
        self._setup_icon_context_menu()

        # ── 标题栏登录按钮（右侧）──
        self._login_user: str = ""
        self._login_btn: QtWidgets.QPushButton | None = None
        # 注入标题栏库内置的服务器配置存储 + 对话框（登录按钮右键菜单打开）
        self.setServerConfigStore(server_config.store(), show_button=False)
        # 跨线程：后台验证通过后经信号回主线程应用登录
        self._login_restore_signal.connect(self._apply_login)
        self._init_login_button()
        QtCore.QTimer.singleShot(0, self._restore_saved_login)

    def _sync_title_bar_from_content(self) -> None:
        icon = self.content_window.windowIcon()
        if not icon.isNull():
            self.setWindowIcon(icon)
            # 设置图标后确保隐藏（l_notepad 不需要显示标题栏图标）
            self.hideTitleBarIcon()
        self._default_title_text = "L Notepad"
        self._title_label = QtWidgets.QLabel(self._default_title_text)
        self._title_label.setStyleSheet(
            "color: #E8EAED; font-weight: 600; font-size: 11px; padding-left: 6px;"
        )
        # 路径过长时在标题栏内自动换行（不增加标题栏高度，限制为两行可见区域）
        self._title_label.setWordWrap(True)
        self._title_label.setMaximumHeight(40)
        self._title_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft
        )
        self._title_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.setStartWidgets([self._title_label])
        # 隐藏库内置 titleLabel（显示 windowTitle），避免与自定义标题重复显示
        builtin_title = getattr(self, "titleLabel", None)
        if builtin_title is not None:
            builtin_title.hide()
        self._apply_title_label_width()
    
    def _setup_icon_context_menu(self) -> None:
        """设置标题栏图标的右键菜单（即使图标隐藏也有效）"""
        
        def setup_menu(menu: QtWidgets.QMenu) -> None:
            """动态构建图标右键菜单"""
            menu.clear()
            
            # 显示/恢复
            act_show = menu.addAction("显示/恢复")
            act_show.triggered.connect(self.show_from_hotkey)
            
            # 最小化到托盘
            act_hide = menu.addAction("最小化到托盘")
            act_hide.triggered.connect(self.showMinimized)
            
            menu.addSeparator()
            
            # 打开文件
            act_open = menu.addAction("打开文件")
            act_open.triggered.connect(self._open_file_from_menu)
            
            # 设置
            act_settings = menu.addAction("设置")
            act_settings.triggered.connect(self._show_settings_dialog)
            
            menu.addSeparator()
            
            # 重启
            act_restart = menu.addAction("重启")
            act_restart.triggered.connect(self._restart_app)
            
            # 退出
            act_quit = menu.addAction("退出")
            act_quit.triggered.connect(self._quit_app)
        
        # 使用回调函数方式，每次右键时动态构建菜单
        self.setIconContextMenuCallback(setup_menu)

    # ── 标题栏登录 ────────────────────────────────────────
    def _init_login_button(self) -> None:
        btn = QtWidgets.QPushButton("登录")
        btn.setObjectName("titleBarLoginButton")
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #E8EAED;"
            " border: 1px solid rgba(255,255,255,0.15); border-radius: 4px;"
            " padding: 2px 10px; font-size: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }"
        )
        btn.clicked.connect(self._on_login_btn_clicked)
        btn.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(self._on_login_btn_context_menu)
        self._login_btn = btn
        self.addEndWidget(btn)
        if btn.parent() is None:
            # 标题栏 end 容器尚未就绪（异常时序兜底）：延迟重试
            QtCore.QTimer.singleShot(0, self._retry_attach_login_btn)
        else:
            lprint("[l_notepad] 登录按钮已加入标题栏")

    def _retry_attach_login_btn(self) -> None:
        btn = self._login_btn
        if btn is None:
            return
        if btn.parent() is None:
            self.addEndWidget(btn)
        if btn.parent() is not None:
            lprint("[l_notepad] 登录按钮延迟加入成功")

    def _on_login_btn_clicked(self) -> None:
        if self._current_login_user():
            self._logout()
        else:
            dlg = LoginDialog(self)
            if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                self._apply_login(dlg._token or "", dlg._username or "")

    def _on_login_btn_context_menu(self, _pos) -> None:
        """右键登录按钮：弹出「服务器设置」入口。"""
        menu = QtWidgets.QMenu(self)
        act_server = menu.addAction("🌐 服务器设置...")
        act_server.triggered.connect(self._show_server_settings_dialog)
        menu.exec(QtGui.QCursor.pos())

    def _show_server_settings_dialog(self) -> None:
        """打开标题栏库内置的「服务器设置」对话框（认证地址 / 认证路由）。"""
        self.showServerSettingsDialog()

    def _current_login_user(self) -> str:
        return getattr(self, "_login_user", "") or ""

    def _account_panel(self):
        return getattr(self.content_window, "_account_favorites_panel", None)

    def _favorite_panels(self) -> list:
        panels = []
        for name in (
            "_folder_favorites_panel",
            "_command_favorites_panel",
            "_url_favorites_panel",
        ):
            panel = getattr(self.content_window, name, None)
            if panel is not None and hasattr(panel, "set_cloud_api"):
                panels.append(panel)
        return panels

    def _apply_login(self, token: str, username: str) -> None:
        """应用登录状态：账号收藏切换到登录用户的 HTTP API，并持久化 token。"""
        panel = self._account_panel()
        if panel is None:
            lprint("[l_notepad] 账号收藏面板未初始化，无法应用登录状态")
            return
        api = NotepadApi(server_config.api_url(), token=token, auth_url=server_config.auth_url())
        panel.api = api
        # 面板报告登录凭据失效时，清除主窗口登录状态
        if hasattr(panel, "login_expired"):
            # 首次调用必然未连接：跳过 disconnect 避免 libpyside RuntimeWarning
            # （该警告走 C 层输出，warnings 过滤器拦不住）；
            # 重复登录时先断开旧连接，防止信号重复连接
            if getattr(self, "_login_expired_connected", False):
                try:
                    panel.login_expired.disconnect(self._on_login_expired)
                except (TypeError, RuntimeError):
                    pass
            panel.login_expired.connect(self._on_login_expired)
            self._login_expired_connected = True
        # 收藏标签页（文件夹/命令/网址）注入云同步 API
        for fav_panel in self._favorite_panels():
            fav_panel.set_cloud_api(api)
        self._login_user = username
        if self._login_btn is not None:
            self._login_btn.setText(username)
            self._login_btn.setToolTip("点击登出")
        try:
            panel.reload_data()
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 登录后加载账号失败: {exc}")
        # 登录成功 → 保存离线缓存（供离线/未登录查看；登出不会覆盖）
        try:
            if hasattr(panel, "_save_offline_cache"):
                panel._save_offline_cache()
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 保存离线账号缓存失败: {exc}")
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKEN_FILE.write_text(
                json.dumps({"token": token, "username": username}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 保存登录 token 失败: {exc}")

    def _logout(self) -> None:
        """登出：账号收藏回退本机免登录（system01），删除持久化 token。"""
        panel = self._account_panel()
        if panel is not None:
            # 无 token → _ensure_api_token 自动走本机免登录；auth_url=1027 直连账号数据
            panel.api = NotepadApi(server_config.api_url(), auth_url=server_config.auth_url())
            try:
                panel.reload_data()
            except Exception as exc:  # noqa: BLE001
                lprint(f"[l_notepad] 登出后重载账号失败: {exc}")
        # 收藏标签页清除云 API（云 item 隐藏）
        for fav_panel in self._favorite_panels():
            try:
                fav_panel.set_cloud_api(None)
            except Exception as exc:  # noqa: BLE001
                lprint(f"[l_notepad] 登出清理云同步失败: {exc}")
        self._login_user = ""
        if self._login_btn is not None:
            self._login_btn.setText("登录")
            self._login_btn.setToolTip("登录 Lugwit 账号")
        try:
            if _TOKEN_FILE.exists():
                _TOKEN_FILE.unlink()
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 删除登录 token 失败: {exc}")

    def _on_login_expired(self) -> None:
        """账号收藏面板报告登录凭据失效：清除主窗口登录状态（弹窗由面板按操作提示）"""
        if not self._current_login_user():
            return
        lprint("[l_notepad] 登录凭据已失效，清除登录状态")
        self._logout()

    def _restore_saved_login(self) -> None:
        """启动时恢复上次登录（先验证 token 有效性，失败则清除）。"""
        if self._current_login_user():
            return
        try:
            if not _TOKEN_FILE.exists():
                return
            data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            token = str(data.get("token") or "").strip()
            username = str(data.get("username") or "").strip()
            if not (token and username):
                return
        except Exception as exc:  # noqa: BLE001
            lprint(f"[l_notepad] 恢复登录状态失败: {exc}")
            return

        def _do_restore() -> None:
            """后台验证 token（避免阻塞启动）。

            区分「token 无效」与「Auth 服务暂不可用」：
            - 明确无效（401/valid=False）→ 清除 token；
            - 服务暂不可用（连接失败/超时）→ 保留 token 并递增间隔重试，
              避免 Auth 服务启动慢于本程序时误删 token 导致每次都要重新登录。
            """
            last_exc: Optional[Exception] = None
            for attempt in range(3):
                try:
                    if authmod.verify_token(token) is None:
                        lprint("[l_notepad] 保存的登录 token 已失效，清除")
                        try:
                            _TOKEN_FILE.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                        return
                    self._login_restore_signal.emit(token, username)
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    lprint(
                        f"[l_notepad] 恢复登录：Auth 服务暂不可用(第{attempt + 1}次) - {exc}")
                    time.sleep(2 + attempt * 2)  # 2s / 4s / 6s
            lprint(
                "[l_notepad] 恢复登录：Auth 服务持续不可用，"
                f"保留 token 待下次重试 ({last_exc})"
            )

        threading.Thread(target=_do_restore, daemon=True).start()

    def _show_settings_dialog(self) -> None:
        """弹出设置对话框"""
        content_window = self.content_window
        
        # 延迟创建 SettingsWidget（首次打开时）
        if content_window._settings_widget is None:
            content_window._settings_widget = SettingsWidget()
            # 初始化字体大小
            if hasattr(content_window._settings_widget, "font_size_spin"):
                content_window._settings_widget.font_size_spin.blockSignals(True)
                content_window._settings_widget.font_size_spin.setValue(content_window._text_font_size)
                content_window._settings_widget.font_size_spin.blockSignals(False)
            # 连接信号
            content_window._settings_widget.indent_display_changed.connect(
                content_window._apply_indent_display_settings
            )
            content_window._settings_widget.folder_hotkey_changed.connect(
                content_window._on_folder_hotkey_button_changed
            )
            content_window._settings_widget.font_size_changed.connect(content_window._set_text_font_size)
        
        settings_widget = content_window._settings_widget
        
        # 延迟创建弹窗（复用已有实例）
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(settings_widget, self)
        
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()
    
    def _open_file_from_menu(self) -> None:
        """从菜单触发打开文件对话框"""
        if hasattr(self.content_window, "_open_external_file"):
            self.content_window._open_external_file()
    
    def _restart_app(self) -> None:
        """重启应用"""
        # 调用 content_window 的重启方法（如果存在）
        if hasattr(self.content_window, "_restart_app"):
            self.content_window._restart_app()
    
    def _quit_app(self) -> None:
        """退出应用"""
        # 获取 QApplication 实例
        app = QtWidgets.QApplication.instance()
        if app:
            # 允许真正关闭（绕过托盘最小化逻辑）
            setattr(self, "_allow_close", True)
            if hasattr(self.content_window, "_allow_close"):
                setattr(self.content_window, "_allow_close", True)
            self.close()
            app.quit()
            # 强制退出，确保 app.quit() 起作用
            QtCore.QTimer.singleShot(100, lambda: sys.exit(0))

    def _apply_title_label_width(self) -> None:
        """根据当前窗口宽度限制标题 label 最大宽度，使长路径在 label 内换行，
        同时让 startContainer.sizeHint 受控，避免触发标题栏横→竖布局切换。"""
        label = getattr(self, "_title_label", None)
        if label is None:
            return
        # 预留系统按钮(约 4×46) + 边距空间
        max_w = max(160, self.width() - 240)
        label.setMaximumWidth(max_w)

    @QtCore.Slot(str)
    def set_title_text(self, text: str) -> None:
        """更新自定义标题栏显示文本；空字符串恢复默认标题。"""
        label = getattr(self, "_title_label", None)
        if label is None:
            return
        display = text.strip() if text else getattr(self, "_default_title_text", "L Notepad")
        label.setText(display)
        label.setToolTip(display)
        self._apply_title_label_width()
        # 标题变长可能需要重新评估按钮横/竖排布局
        if hasattr(self, "_updateLayout"):
            self._updateLayout()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        # 窗口宽度变化时同步刷新标题 label 的最大宽度，保证换行点跟随窗口
        self._apply_title_label_width()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        # 无边框窗口下，作为子部件嵌入的 content_window(QWidget) 显示时有时不会
        # 收到绘制事件（旧的 QMainWindow 实现会自动触发），导致内容区空白直到一次
        # resize/交互。首次显示、以及从托盘隐藏后重新显示，都需要强制重排并重绘，
        # 因此每次 showEvent 都触发，不能只做一次。
        QtCore.QTimer.singleShot(0, self._force_content_repaint)

    def _force_content_repaint(self) -> None:
        cw = getattr(self, "content_window", None)
        if cw is None:
            return
        # content_window 作为内容窗口在「关闭到托盘」时，其自身 closeEvent 会调用
        # self.hide() 把这个子部件隐藏（standalone 模式下用于隐藏到托盘）。嵌入到
        # 外壳里时，外壳重新显示后必须把它重新设为可见，否则内容区一直空白。
        if cw.isHidden():
            cw.show()
        lay = cw.layout()
        if lay is not None:
            lay.activate()
        cw.updateGeometry()
        cw.repaint()
        for child in cw.findChildren(QtWidgets.QWidget):
            try:
                # QAbstractScrollArea 子类（QListWidget/QTextEdit/QScrollArea 等）的
                # update() 在 PySide6 中只有带参重载（update(x,y,w,h)/update(QRect)），
                # 无参 update() 会抛 TypeError，需改用 viewport().update()
                if isinstance(child, QtWidgets.QAbstractScrollArea):
                    child.viewport().update()
                else:
                    child.update()
            except Exception:
                # 个别控件可能在隐藏到托盘时已被 C++ 侧销毁，跳过避免拖垮恢复
                pass

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # 先让 content_window 处理 closeEvent（自动保存等）
        self.content_window.closeEvent(event)
        
        # 如果事件被接受（真正关闭），则直接返回
        if event.isAccepted():
            return
        
        # 如果事件被忽略（最小化到托盘），则隐藏窗口
        if not event.isAccepted():
            self.hide()
            event.ignore()

    @QtCore.Slot()
    def show_from_hotkey(self) -> None:
        # 先清除最小化状态，再正常显示
        if self.isMinimized() or bool(self.windowState() & QtCore.Qt.WindowState.WindowMinimized):
            self.setWindowState(
                (self.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
                | QtCore.Qt.WindowState.WindowActive
            )
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # Windows 下绕过前台锁定，真正把窗口提到最前
        self._force_foreground()
        if hasattr(self.content_window, "show_from_hotkey"):
            self.content_window.show_from_hotkey()
        # 从最小化/隐藏恢复时，子内容区可能拿不到绘制事件而空白，强制重绘一次
        QtCore.QTimer.singleShot(0, self._force_content_repaint)

    def _force_foreground(self) -> None:
        """绕过 Windows 前台锁定，把本窗口强制提到前台。

        全局热键唤起时本进程通常不是前台进程，单纯 SetForegroundWindow 会被
        系统拒绝（任务栏闪烁但不前置）。这里临时把前台锁定超时设为 0 再提升，
        完成后还原。

        注意：不要用 AttachThreadInput——它会合并本线程与外部前台 app 线程的
        输入队列，导致窗口显示后鼠标点击被错误路由（点不动）。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            if not hwnd:
                return

            SW_RESTORE = 9
            SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
            SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
            SPIF_SENDCHANGE = 0x0002

            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)

            # 临时取消前台锁定超时
            old_timeout = ctypes.c_uint(0)
            user32.SystemParametersInfoW(
                SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old_timeout), 0
            )
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE
            )
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                # 还原原超时设置
                user32.SystemParametersInfoW(
                    SPI_SETFOREGROUNDLOCKTIMEOUT,
                    0,
                    ctypes.c_void_p(int(old_timeout.value)),
                    SPIF_SENDCHANGE,
                )
        except Exception:
            pass


    @QtCore.Slot(int)
    def show_folder_favorites_from_hotkey(self, caller_hwnd: int = 0) -> None:
        # 先清除最小化状态，避免随后内容窗口里的 showNormal() 用最小化前的几何
        # 覆盖掉我们设置的收藏夹尺寸/位置。
        if self.isMinimized() or bool(self.windowState() & QtCore.Qt.WindowState.WindowMinimized):
            self.setWindowState(
                (self.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
                | QtCore.Qt.WindowState.WindowActive
            )
        # 由内容窗口先把收藏夹的尺寸(400)、位置(贴鼠标)、标签都算好并应用，
        # 再在其内部 _bring_to_front 显示窗口——即“先设置好大小和位置再显示”。
        # 注意：不要调用 self.show_from_hotkey()，它会先显示旧尺寸窗口，
        # 且会把宽度恢复成正常宽度(980)，与收藏夹的 400 冲突。
        self.content_window.show_folder_favorites_from_hotkey(caller_hwnd)
        # 窗口已显示在正确几何后，再抢占前台并强制重绘内容区
        self._force_foreground()
        QtCore.QTimer.singleShot(0, self._force_content_repaint)

    def set_tray_icon(self, tray: QtWidgets.QSystemTrayIcon | None) -> None:
        self.content_window.set_tray_icon(tray)


    @QtCore.Slot(str)
    def _handle_file_open_request(self, file_path: str) -> None:
        from .logger import log
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            log(f"IPC 打开文件失败：{file_path}", level="ERROR")
            return
        self.show_from_hotkey()
        try:
            if hasattr(self.content_window, "add_ipc_file"):
                self.content_window.add_ipc_file(str(path))
            else:
                self.content_window._set_external_file_editor(str(path))
            log(f"IPC 打开文件：{file_path}", level="INFO")
        except Exception as exc:
            log(f"IPC 打开文件异常：{exc}", level="ERROR")

    def __getattr__(self, name: str):
        # 防递归：窗口构建早期 content_window 尚未赋值时，Qt 已回调
        # nativeEvent/resizeEvent 访问 _wasMinimized/_title_label 等属性，
        # 若直接 self.content_window 会再次触发 __getattr__ 无限递归；
        # dunder 属性不代理，避免 PySide6 内部探测引发意外行为
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            content_window = object.__getattribute__(self, "content_window")
        except AttributeError:
            raise AttributeError(name)
        if content_window is None:
            raise AttributeError(name)
        return getattr(content_window, name)



def _foreground_process_name() -> str:
    """返回当前前台窗口所属进程的可执行文件名（小写）。失败返回空串。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        ]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


# 远程桌面/远控客户端进程名：当本机前台是这些窗口时，按键实际发往远程会话，
# 本机的全局热键应抑制，避免远程与本地两台 l_notepad 同时被唤起。
_REMOTE_CLIENT_EXES = {
    "mstsc.exe",          # 微软远程桌面
    "msrdc.exe",          # Windows App / RD client
    "cmrcviewer.exe",     # SCCM 远程控制
    "vncviewer.exe",
    "tvnviewer.exe",
    "anydesk.exe",
    "sunloginclient.exe",  # 向日葵
    "todesk.exe",
    "rustdesk.exe",
    "gameviewer.exe",     # 网易UU远程
}

# 运行时从配置追加的远控客户端进程名（小写），由 main() 从 QSettings 读取填充。
# 用于网易UU远程等默认集合未覆盖的工具：把其 exe 名加进配置即可，无需改代码。
_EXTRA_REMOTE_CLIENTS: set[str] = set()


def _is_remote_client_name(name: str) -> bool:
    """给定进程名（小写）是否为远程桌面/远控客户端。"""
    return bool(name) and (
        name in _REMOTE_CLIENT_EXES or name in _EXTRA_REMOTE_CLIENTS
    )


def _foreground_is_remote_client() -> bool:
    """前台窗口是否为远程桌面/远控客户端（按键将发往远程会话）。"""
    return _is_remote_client_name(_foreground_process_name())


# 崩溃日志路径（由 _install_crash_handlers 设置，Qt fatal 也写这里）
_CRASH_LOG_PATH: Path | None = None


def _install_crash_handlers() -> None:
    """安装崩溃/未捕获异常兜底，写 <日志目录>/crash_<日期>.log。

    - faulthandler：捕获 Python 层原生崩溃(segfault)时的 Python 调用栈
    - sys.excepthook：记录未捕获的 Python 异常（含 PySide 对象已删除等）
    - 崩溃日志独立于 notepad_console.log，保留现场；日志目录统一到
      D:\\Temp\\Log\\l_notepad（可用 L_NOTEPAD_LOG_DIR 覆盖），不进源码包
    """
    global _CRASH_LOG_PATH
    try:
        crash_root = Path(
            os.environ.get("L_NOTEPAD_LOG_DIR", r"D:\Temp\Log\l_notepad")
        )
        crash_root.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG_PATH = crash_root / f"crash_{time.strftime('%Y%m%d')}.log"
        with _CRASH_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(
                f"\n===== 进程启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"pid={os.getpid()} =====\n"
            )
        try:
            import faulthandler
            faulthandler.enable(_CRASH_LOG_PATH.open("a", encoding="utf-8"))
        except Exception:
            pass
    except Exception:
        _CRASH_LOG_PATH = None

    def _excepthook(tp, val, tb) -> None:
        try:
            import traceback
            traceback.print_exception(tp, val, tb)
            if _CRASH_LOG_PATH is not None:
                with _CRASH_LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(
                        f"\n===== 未捕获异常 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                    traceback.print_exception(tp, val, tb, file=f)
        except Exception:
            pass
        sys.__excepthook__(tp, val, tb)

    sys.excepthook = _excepthook


def _install_qt_message_filter() -> None:
    """过滤已知无害的 Qt 内部警告，避免刷屏；其余消息照常输出到 stderr。

    - ``QFont::setPointSize: Point size <= 0``：样式表用像素字号时 pointSize()==-1，
      Qt 内部字体传播偶发触发，无实际影响。
    - QPushButton ... ``does not have a property named "alignment"``：兜底，
      防止其它 QSS 再次触发（主因已在 style.qss 移除）。
    """

    def _handler(mode, context, message) -> None:
        # Qt 致命/严重错误：同时写入崩溃日志（即使 stderr 被覆盖也能保留现场）
        try:
            if int(mode) in (2, 3):  # QtCriticalMsg / QtFatalMsg
                if _CRASH_LOG_PATH is not None:
                    try:
                        with _CRASH_LOG_PATH.open("a", encoding="utf-8") as f:
                            f.write(
                                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [Qt] "
                                f"{message}\n")
                    except Exception:
                        pass
        except Exception:
            pass
        if "QFont::setPointSize: Point size <= 0" in message:
            return
        if "does not have a property named" in message and '"alignment"' in message:
            return
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            pass

    QtCore.qInstallMessageHandler(_handler)


def main(use_frameless: bool = True) -> int:
    _install_crash_handlers()
    _install_qt_message_filter()
    _set_windows_appid("Lugwit.l_notepad.pc")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # PC 客户端不再启动本地 backend_server 子进程：账号/收藏/远程日志等
    # 服务统一走远端 nginx 入口（见 server_config），本机 8765 后端由服务侧
    # 自行部署；客户端启动不再拉起额外 Python 进程（减少冷启动 CPU/磁盘尖峰）。
    try:
        from l_qt_wgt_lib import install_combobox_wheel_guard

        install_combobox_wheel_guard(app)
    except Exception:
        pass

    icon_path = Path(__file__).resolve().parent / "static" / "favicon.svg"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    api = LocalNotepadApi()

    restart_module = "l_notepad_client.local_main" if use_frameless else "l_notepad_client.local_main_ori"

    def _restart_process() -> None:
        src_dir = str(Path(__file__).resolve().parents[1])
        ok = QtCore.QProcess.startDetached(
            sys.executable,
            ["-m", restart_module],
            src_dir,
        )
        if not ok:
            raise RuntimeError("QProcess.startDetached failed")

    settings = QtCore.QSettings("Lugwit", "l_notepad_pc")
    max_gap_raw = settings.value("hotkey/double_ctrl_max_gap_sec", str(DOUBLE_CTRL_MAX_GAP_SEC))
    try:
        max_gap_sec = float(str(max_gap_raw))
    except Exception:
        max_gap_sec = DOUBLE_CTRL_MAX_GAP_SEC
    max_gap_sec = max(DOUBLE_CTRL_MIN_GAP_SEC, min(1.0, max_gap_sec))

    hotkey_key = str(settings.value("hotkey/double_key", DEFAULT_HOTKEY_KEY))
    if hotkey_key not in HOTKEY_OPTIONS:
        hotkey_key = DEFAULT_HOTKEY_KEY

    # 追加远控客户端进程名（逗号/分号/空白分隔），用于覆盖默认集合未含的工具（如网易UU远程）。
    extra_clients_raw = str(
        settings.value("hotkey/extra_remote_clients", "") or ""
    )
    for tok in re.split(r"[,;\s]+", extra_clients_raw):
        tok = tok.strip().lower()
        if tok:
            _EXTRA_REMOTE_CLIENTS.add(tok if tok.endswith(".exe") else tok + ".exe")
    if _EXTRA_REMOTE_CLIENTS:
        logging.info(f"远控客户端追加识别: {sorted(_EXTRA_REMOTE_CLIENTS)}")

    hotkey_ref: dict[str, DoubleKeyWatcher] = {}

    def _update_hotkey_interval(value: float) -> None:
        watcher = hotkey_ref.get("watcher")
        if watcher is not None:
            watcher.update_interval(value)

    def _update_hotkey_key(key: str) -> None:
        watcher = hotkey_ref.get("watcher")
        if watcher is not None:
            watcher.update_key(key)

    if use_frameless:
        win = MainWindow(
            api,
            restart_callback=_restart_process,
            hotkey_interval_callback=_update_hotkey_interval,
            hotkey_key_callback=_update_hotkey_key,
        )
    else:
        # 原始模式：直接显示内容窗口（QWidget 顶层），使用系统原生标题栏，
        # 不套自定义无边框外壳，便于排查自定义标题栏相关问题。
        win = NotepadContentWindow(
            api,
            restart_callback=_restart_process,
            hotkey_interval_callback=_update_hotkey_interval,
            hotkey_key_callback=_update_hotkey_key,
        )
        win.setWindowFlags(QtCore.Qt.WindowType.Window)
        win.setWindowTitle("L Notepad")
        win.resize(980, 640)
        win.setMinimumSize(480, 360)
    tray = _setup_tray_icon(app, win, app.windowIcon())
    win.set_tray_icon(tray)

    server = QtNetwork.QLocalServer()
    try:
        QtNetwork.QLocalServer.removeServer(IPC_SERVER_NAME)
    except Exception:
        pass
    if not server.listen(IPC_SERVER_NAME):
        lprint(f"IPC 服务启动失败: {server.errorString()}")
    else:
        lprint(f"IPC 服务已启动: {IPC_SERVER_NAME}")

    def _handle_ipc_ready_read() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                continue

            def _read_client(c=conn):
                if not c.waitForReadyRead(1000):
                    c.disconnectFromServer()
                    c.deleteLater()
                    return
                raw = bytes(c.readAll()).decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw else {}
                except Exception:
                    data = {}
                files = data.get("files", []) if isinstance(data, dict) else []
                for f in files:
                    QtCore.QMetaObject.invokeMethod(
                        win,
                        "_handle_file_open_request",
                        QtCore.Qt.ConnectionType.QueuedConnection,
                        QtCore.Q_ARG(str, str(f)),
                    )
                c.disconnectFromServer()
                c.deleteLater()

            QtCore.QTimer.singleShot(0, _read_client)

    server.newConnection.connect(_handle_ipc_ready_read)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    _remove_unsafe_stream_handlers(root_logger)
    logging.getLogger("l_folder_favorites").setLevel(logging.WARNING)

    from .logger import log, setup

    # console 日志统一写到数据盘 D:\Temp\Log\l_notepad（可用 L_NOTEPAD_LOG_DIR 覆盖），
    # 不写入源码包（避免污染发布包、被更新覆盖、只读部署时失败）
    log_dir = Path(
        os.environ.get("L_NOTEPAD_LOG_DIR", r"D:\Temp\Log\l_notepad")
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_dir / "notepad_console.log"
    # 每次启动清空 console 日志（覆盖模式）：日志窗口的 FileLogTailer 每次启动
    # 从头重读整个文件，保留历史会把旧崩溃 traceback 反复回放，这里直接截断
    log_file_handle = log_file_path.open("w", encoding="utf-8")
    setup(log_file_path)
    file_handler = SafeFileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root_logger.addHandler(file_handler)
    log(f"console log file: {log_file_path}")
    sys.stdout = TeeStream(sys.stdout, log_file_handle)
    sys.stderr = TeeStream(sys.stderr, log_file_handle)
    win.set_console_log_path(str(log_file_path))

    ff_hotkey = FolderFavoritesHotkeyService(win)

    def _on_ff_hotkey_triggered() -> None:
        # 前台是远程桌面客户端时抑制：按键实际发往远程会话，避免两台同时唤起。
        fg = _foreground_process_name()
        if _is_remote_client_name(fg):
            logging.info(f"前台为远程客户端({fg})，抑制本机 Ctrl+鼠标 热键")
            return
        logging.info(f"Ctrl+鼠标 热键触发，前台进程={fg or '?'}")
        # 关键：必须在唤起/激活本窗口之前捕获调用者窗口（此刻 l_notepad 还未被激活）。
        #
        # 注意：不能用 GetForegroundWindow()。l_notepad 被唤起后通常一直是前台进程，
        # 用户把鼠标移到目标程序上按中键时（未点击目标程序），前台仍是 l_notepad，
        # 会把调用者误识别成自身（日志里 program='python.exe' 即此现象）。
        #
        # 因此改为按「鼠标指针下的窗口」识别（Ctrl+中键 = 指向哪个窗口就操作哪个），
        # 并用 GetAncestor(GA_ROOT) 提升到顶层根窗口（避免命中 Explorer 地址栏、
        # 浏览器标签等子控件，否则 Shell COM 匹配不到地址栏路径）；失败再回退前台窗口。
        caller_hwnd = 0
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
                user32.WindowFromPoint.argtypes = [wintypes.POINT]
                user32.WindowFromPoint.restype = wintypes.HWND
                user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
                user32.GetAncestor.restype = wintypes.HWND
                pt = wintypes.POINT()
                if user32.GetCursorPos(ctypes.byref(pt)):
                    hwnd_at = int(user32.WindowFromPoint(pt) or 0)
                    if hwnd_at:
                        root = int(user32.GetAncestor(hwnd_at, 2) or 0)  # GA_ROOT=2
                        caller_hwnd = root if root else hwnd_at
                if not caller_hwnd:
                    caller_hwnd = int(user32.GetForegroundWindow())
            except Exception:
                caller_hwnd = 0
        QtCore.QMetaObject.invokeMethod(
            win,
            "show_folder_favorites_from_hotkey",
            QtCore.Qt.ConnectionType.QueuedConnection,
            QtCore.Q_ARG(int, caller_hwnd),
        )

    def _on_ff_hotkey_started(ok: bool) -> None:
        if ok:
            logging.info("文件夹收藏全局快捷键已启动 (Ctrl+鼠标)")
        else:
            logging.info("文件夹收藏全局快捷键未启动")

    def _on_ff_hotkey_failed(message: str) -> None:
        logging.warning(message)

    def _on_win_v_hotkey_triggered() -> None:
        """Win+V：捕获调用者窗口，弹出「仅剪贴板历史」小窗口。"""
        fg = _foreground_process_name()
        if _is_remote_client_name(fg):
            logging.info(f"前台为远程客户端({fg})，抑制本机 Win+V 热键")
            return
        logging.info("Win+V 热键触发，准备弹出剪贴板历史")
        caller_hwnd = 0
        if sys.platform == "win32":
            try:
                import ctypes

                caller_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            except Exception:
                caller_hwnd = 0
        panel = None
        content = getattr(win, "content_window", None)
        if content is not None:
            panel = getattr(content, "_folder_favorites_panel", None)
        if panel is None:
            try:
                panels = win._favorite_panels()
                panel = panels[0] if panels else None
            except Exception:
                panel = None
        if panel is not None and hasattr(panel, "popup_clipboard_history"):
            panel.popup_clipboard_history(caller_hwnd)
        else:
            logging.warning("Win+V：未找到文件夹收藏面板，无法弹出剪贴板历史")

    def _update_folder_favorites_hotkey(button: str) -> None:
        ff_hotkey.set_trigger_button(button)
        label = "中键" if button == "middle" else "左键"
        logging.info(f"文件夹收藏快捷键已切换为 Ctrl+{label}")

    win._folder_favorites_hotkey_callback = _update_folder_favorites_hotkey
    ff_hotkey.started.connect(_on_ff_hotkey_started)
    ff_hotkey.failed.connect(_on_ff_hotkey_failed)
    ff_hotkey.start(_on_ff_hotkey_triggered)
    # Win+V：接管系统剪贴板历史，弹出 l_notepad 自己的剪贴板历史小窗口
    ff_hotkey.win_v_triggered.connect(_on_win_v_hotkey_triggered)

    def _log_double_key(message: str, level: int = logging.INFO) -> None:
        logging.getLogger(__name__).log(level, message)

    def _on_double_key_triggered() -> None:
        # 前台是远程桌面客户端时抑制：按键实际发往远程会话，避免两台同时唤起。
        fg = _foreground_process_name()
        if _is_remote_client_name(fg):
            logging.info(f"前台为远程客户端({fg})，抑制本机双击热键")
            return
        logging.info(f"双击热键触发，前台进程={fg or '?'}")
        QtCore.QMetaObject.invokeMethod(
            win, "show_from_hotkey", QtCore.Qt.ConnectionType.QueuedConnection
        )

    hotkey = DoubleKeyWatcher(
        _on_double_key_triggered,
        log_callback=_log_double_key,
        min_gap_sec=DOUBLE_CTRL_MIN_GAP_SEC,
        max_gap_sec=max_gap_sec,
        hotkey_key=hotkey_key,
    )
    hotkey_ref["watcher"] = hotkey
    win.show()
    code = int(app.exec())
    hotkey.release()
    ff_hotkey.stop()
    if tray is not None:
        tray.hide()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
