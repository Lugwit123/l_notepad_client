# -*- coding: utf-8 -*-
"""事件驱动的剪贴板捕获引擎。

Windows：专用线程注册隐藏窗口 + ``AddClipboardFormatListener`` + 自有消息泵，
``WM_CLIPBOARDUPDATE`` 到达时在该线程就地读取剪贴板 —— 不依赖 GUI 线程空闲，
不使用任何周期性轮询（空闲时线程阻塞在 ``GetMessage``，零 CPU）。

其他平台：回退到 ``QClipboard.dataChanged`` 订阅，复用同一套解析与去重。

读取失败（``OpenClipboard`` 被占用 / 所有者未响应）会挂一条 ``WM_TIMER`` 单次
重试链（最多 3 次，约 60/120/240ms 退避）；重试前剪贴板序列号已变化则放弃旧内容。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import os
import struct
import sys
import threading
import time
from typing import Callable, Optional

from PySide6 import QtCore, QtGui, QtWidgets

from .clipboard_store import (
    CLIPBOARD_IMAGE_MAX_SIDE,
    dedupe_key,
    images_dir,
)
from pytracemp import lprint

# 图片预览文本（图文混排时保留的真实文本）的截断长度
_IMAGE_TEXT_LIMIT = 500

# 自产剪贴板写入的抑制窗口：本应用写剪贴板后这段时间内的同内容捕获视为回环
_SELF_WRITE_WINDOW_SEC = 1.5

# 读取失败重试策略
_RETRY_MAX = 3
_RETRY_BASE_MS = 60

# 诊断计数日志节流
_LOG_EVERY = 20

_IS_WINDOWS = sys.platform == "win32"


def _new_counters() -> dict:
    return {
        "updates_total": 0,
        "retry_success": 0,
        "read_failures": 0,
        "superseded": 0,
        "unknown_format": 0,
        "self_write_suppressed": 0,
    }


# ── 条目构造（Windows 与回退路径共用）──

def _text_item(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    return {"kind": "text", "text": text}


def _file_item(files: list) -> Optional[dict]:
    normalized: list[str] = []
    seen: set[str] = set()
    for f in files:
        try:
            local = os.path.normpath(str(f))
        except Exception:
            continue
        if not local or local in seen:
            continue
        if not os.path.exists(local):
            continue
        seen.add(local)
        normalized.append(local)
    if not normalized:
        return None
    if len(normalized) == 1:
        display = os.path.basename(normalized[0]) or normalized[0]
    else:
        display = f"{len(normalized)} 个文件"
    return {
        "kind": "file",
        "text": display,
        "files": normalized,
        "count": len(normalized),
    }


def _image_item_from_png(
    png_bytes: bytes, text: str, width: int, height: int
) -> Optional[tuple[dict, bytes]]:
    if not png_bytes:
        return None
    md5 = hashlib.md5(png_bytes).hexdigest()[:16]
    display = "[图片]"
    if text and text.strip():
        display = text.strip()
        if len(display) > _IMAGE_TEXT_LIMIT:
            display = display[:_IMAGE_TEXT_LIMIT] + "…"
    item = {
        "kind": "image",
        "text": display,
        "md5": md5,
        "image_path": str(images_dir() / f"{md5}.png"),
        "width": int(width),
        "height": int(height),
    }
    return item, png_bytes


def _encode_image(image: QtGui.QImage, text: str) -> Optional[tuple[dict, bytes]]:
    """QImage → PNG 字节（超长边等比缩放），返回 (条目, PNG 字节)。"""
    if image.isNull():
        return None
    if max(image.width(), image.height()) > CLIPBOARD_IMAGE_MAX_SIDE:
        image = image.scaled(
            CLIPBOARD_IMAGE_MAX_SIDE, CLIPBOARD_IMAGE_MAX_SIDE,
            QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
        )
    buffer = QtCore.QByteArray()
    qbuf = QtCore.QBuffer(buffer)
    qbuf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    try:
        if not image.save(qbuf, "PNG"):
            return None
    finally:
        qbuf.close()
    return _image_item_from_png(
        bytes(buffer), text, image.width(), image.height())


def _encode_image_png(
    png_bytes: bytes, text: str
) -> Optional[tuple[dict, bytes]]:
    """来源已提供 PNG：尺寸在限内则直接沿用原字节（不重新编码）。"""
    image = QtGui.QImage.fromData(png_bytes, "PNG")
    if image.isNull():
        return None
    if max(image.width(), image.height()) > CLIPBOARD_IMAGE_MAX_SIDE:
        return _encode_image(image, text)
    return _image_item_from_png(png_bytes, text, image.width(), image.height())


def _dib_to_qimage(dib: bytes) -> Optional[QtGui.QImage]:
    """CF_DIB / CF_DIBV5 字节 + 14 字节 BMP 文件头 → QImage。"""
    if not dib or len(dib) < 40:
        return None
    header = struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, 14)
    image = QtGui.QImage.fromData(header + dib, "BMP")
    return None if image.isNull() else image


class _SelfWriteGuard:
    """记录本应用刚写进剪贴板的内容，避免把自己的写入又录成一条历史。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[tuple[tuple, float]] = []

    def register(self, key: tuple) -> None:
        now = time.monotonic()
        with self._lock:
            self._entries = [
                (k, t) for k, t in self._entries
                if now - t < _SELF_WRITE_WINDOW_SEC
            ]
            self._entries.append((key, now))

    def should_suppress(self, key: tuple) -> bool:
        now = time.monotonic()
        with self._lock:
            self._entries = [
                (k, t) for k, t in self._entries
                if now - t < _SELF_WRITE_WINDOW_SEC
            ]
            for k, _t in self._entries:
                if k == key:
                    return True
                # ("image", "*")：图片 md5 无法预先得知，按类型整体抑制
                if k[1] == "*" and k[0] == key[0]:
                    return True
        return False


class ClipboardRecorder:
    """捕获引擎门面：生命周期、自写抑制、诊断计数。"""

    _instance: Optional["ClipboardRecorder"] = None

    @classmethod
    def instance(cls) -> "ClipboardRecorder":
        if cls._instance is None:
            cls._instance = ClipboardRecorder()
        return cls._instance

    def __init__(self) -> None:
        self.counters = _new_counters()
        self._counter_lock = threading.Lock()
        self._on_capture: Optional[Callable] = None
        self._last_key: Optional[tuple] = None
        self._guard = _SelfWriteGuard()
        self._lock = threading.Lock()
        self._listener = None
        self._fallback = None

    # ── 生命周期 ──
    def start(self, on_capture: Optional[Callable] = None) -> None:
        """启动捕获（幂等）。``on_capture(item, png_bytes)`` 在捕获线程被调用。"""
        if on_capture is not None:
            self._on_capture = on_capture
        with self._lock:
            if self._listener is not None or self._fallback is not None:
                return
            if _IS_WINDOWS:
                listener = _Win32ClipboardListener(self)
                self._listener = listener
            else:
                fallback = _QtClipboardFallback(self)
                self._fallback = fallback
        if _IS_WINDOWS:
            listener.start()
            if not listener.wait_ready(3.0) or not listener.ready_ok():
                lprint("剪贴板监听线程启动失败，剪贴板历史将不会记录")
        else:
            fallback.start()

    def stop(self, timeout: float = 3.0) -> None:
        """停止捕获（幂等）：反注册监听、销毁窗口、退出消息泵并 join。"""
        with self._lock:
            listener, self._listener = self._listener, None
            fallback, self._fallback = self._fallback, None
        if fallback is not None:
            try:
                fallback.stop()
            except Exception as e:
                lprint(f"停止剪贴板回退捕获失败: {e}")
        if listener is not None:
            if not listener.stop(timeout):
                lprint("剪贴板监听线程未在超时内退出")

    @property
    def active(self) -> bool:
        return self._listener is not None or self._fallback is not None

    # ── 自产写入 ──
    def write_to_clipboard(
        self,
        text: Optional[str] = None,
        image: Optional[QtGui.QImage] = None,
        files: Optional[list] = None,
    ) -> None:
        """应用内写系统剪贴板的唯一入口，同时登记抑制键避免回环录制。"""
        clipboard = QtWidgets.QApplication.clipboard()
        if files:
            normalized = [os.path.normpath(str(f)) for f in files if f]
            if not normalized:
                return
            urls = [QtCore.QUrl.fromLocalFile(f) for f in normalized]
            mime = QtCore.QMimeData()
            mime.setUrls(urls)
            self._guard.register(("file", "|".join(sorted(normalized))))
            clipboard.setMimeData(mime)
            return
        if image is not None and not image.isNull():
            self._guard.register(("image", "*"))
            clipboard.setImage(image)
            return
        if text is None:
            return
        self._guard.register(("text", str(text)))
        clipboard.setText(str(text))

    # ── 诊断 ──
    def diagnostics(self) -> dict:
        with self._counter_lock:
            return dict(self.counters)

    def _bump(self, name: str, n: int = 1, log: bool = False) -> None:
        with self._counter_lock:
            self.counters[name] = self.counters.get(name, 0) + n
            value = self.counters[name]
            snapshot = dict(self.counters)
        if log or value == 1 or value % _LOG_EVERY == 0:
            lprint(f"剪贴板捕获统计: {snapshot}")

    # ── 捕获结果出口（捕获线程调用）──
    def _emit(self, item: Optional[dict], png_bytes: Optional[bytes]) -> None:
        if not item:
            return
        key = dedupe_key(item)
        if self._guard.should_suppress(key):
            self._bump("self_write_suppressed")
            return
        if key == self._last_key:
            # 同一内容被重复上报（Qt 信号重入 / 同一序列号重复读取）
            return
        self._last_key = key
        callback = self._on_capture
        if callback is None:
            return
        try:
            callback(item, png_bytes)
        except Exception as e:
            lprint(f"提交剪贴板条目失败: {e}")


# ── Windows：专用线程 + AddClipboardFormatListener + 消息泵 ──

if _IS_WINDOWS:
    _WM_CLIPBOARDUPDATE = 0x031D
    _WM_TIMER = 0x0113
    _WM_CLOSE = 0x0010
    _WM_DESTROY = 0x0002

    _CF_TEXT = 1
    _CF_DIB = 8
    _CF_UNICODETEXT = 13
    _CF_HDROP = 15
    _CF_DIBV5 = 17

    _WS_POPUP = 0x80000000
    _RETRY_TIMER_ID = 1
    _CLASS_NAME = "LNotepadClipboardListener"

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _shell32 = ctypes.windll.shell32

    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT,
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.wintypes.WPARAM,
        ctypes.wintypes.LPARAM,
    )

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.wintypes.HINSTANCE),
            ("hIcon", ctypes.wintypes.HICON),
            ("hCursor", ctypes.wintypes.HANDLE),
            ("hbrBackground", ctypes.wintypes.HBRUSH),
            ("lpszMenuName", ctypes.wintypes.LPCWSTR),
            ("lpszClassName", ctypes.wintypes.LPCWSTR),
        ]

    def _init_win32_prototypes() -> None:
        _HWND = ctypes.wintypes.HWND
        _LPVOID = ctypes.c_void_p
        _user32.GetMessageW.argtypes = [
            ctypes.POINTER(ctypes.wintypes.MSG),
            _HWND,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        _user32.GetMessageW.restype = ctypes.c_int
        _user32.TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
        _user32.DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
        _user32.DispatchMessageW.restype = _LRESULT
        _user32.DefWindowProcW.argtypes = [
            _HWND,
            ctypes.c_uint,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
        _user32.DefWindowProcW.restype = _LRESULT
        _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
        _user32.RegisterClassW.restype = ctypes.wintypes.ATOM
        _user32.UnregisterClassW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.HINSTANCE]
        _user32.CreateWindowExW.argtypes = [
            ctypes.c_uint,          # dwExStyle
            ctypes.wintypes.LPCWSTR,  # lpClassName
            ctypes.wintypes.LPCWSTR,  # lpWindowName
            ctypes.c_uint,          # dwStyle
            ctypes.c_int,           # X
            ctypes.c_int,           # Y
            ctypes.c_int,           # nWidth
            ctypes.c_int,           # nHeight
            _HWND,                  # hWndParent
            ctypes.wintypes.HMENU,  # hMenu
            ctypes.wintypes.HINSTANCE,  # hInstance
            _LPVOID,                # lpParam
        ]
        _user32.CreateWindowExW.restype = _HWND
        _user32.DestroyWindow.argtypes = [_HWND]
        _user32.PostMessageW.argtypes = [
            _HWND, ctypes.c_uint, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM
        ]
        _user32.PostQuitMessage.argtypes = [ctypes.c_int]
        _user32.AddClipboardFormatListener.argtypes = [_HWND]
        _user32.RemoveClipboardFormatListener.argtypes = [_HWND]
        _user32.OpenClipboard.argtypes = [_HWND]
        _user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        _user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        _user32.GetClipboardData.argtypes = [ctypes.c_uint]
        _user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
        _user32.SetTimer.argtypes = [_HWND, ctypes.c_size_t, ctypes.c_uint, _LPVOID]
        _user32.SetTimer.restype = ctypes.wintypes.UINT
        _user32.KillTimer.argtypes = [_HWND, ctypes.c_size_t]
        _user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        _user32.RegisterClipboardFormatW.argtypes = [ctypes.wintypes.LPCWSTR]
        _kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE
        _kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
        _kernel32.GlobalLock.restype = ctypes.c_void_p
        _kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
        _kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
        _kernel32.GlobalSize.restype = ctypes.c_size_t
        _kernel32.GlobalSize.argtypes = [ctypes.wintypes.HGLOBAL]
        _shell32.DragQueryFileW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.wintypes.LPWSTR,
            ctypes.c_uint,
        ]
        _shell32.DragQueryFileW.restype = ctypes.c_uint

    _init_win32_prototypes()

    class _Win32ClipboardListener(threading.Thread):
        """独立线程的消息泵：注册剪贴板监听并在事件到达时就地读取。"""

        def __init__(self, recorder: ClipboardRecorder) -> None:
            super().__init__(name="clipboard-listener", daemon=True)
            self._rec = recorder
            self._hwnd = 0
            self._ready = threading.Event()
            self._ok = False
            self._last_seq = -1
            self._retry_seq = 0
            self._retry_left = 0
            self._retry_delay = _RETRY_BASE_MS
            self._png_format = 0
            self._wndproc = _WNDPROC(self._wnd_proc)

        # ── 生命周期 ──
        def wait_ready(self, timeout: float) -> bool:
            return bool(self._ready.wait(timeout))

        def ready_ok(self) -> bool:
            return self._ok

        def stop(self, timeout: float = 3.0) -> bool:
            hwnd = self._hwnd
            if hwnd:
                try:
                    _user32.RemoveClipboardFormatListener(hwnd)
                    _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                except Exception as e:
                    lprint(f"停止剪贴板监听失败: {e}")
            self.join(timeout)
            return not self.is_alive()

        def run(self) -> None:
            hinst = _kernel32.GetModuleHandleW(None)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS_NAME
            try:
                if not _user32.RegisterClassW(ctypes.byref(wc)):
                    lprint("剪贴板监听窗口类注册失败")
                    self._ready.set()
                    return
                self._hwnd = _user32.CreateWindowExW(
                    0, _CLASS_NAME, _CLASS_NAME, _WS_POPUP,
                    0, 0, 0, 0, 0, 0, hinst, None,
                )
                if not self._hwnd:
                    lprint("剪贴板监听窗口创建失败")
                    self._ready.set()
                    return
                self._png_format = _user32.RegisterClipboardFormatW("PNG")
                if not _user32.AddClipboardFormatListener(self._hwnd):
                    lprint("AddClipboardFormatListener 失败")
                    self._ready.set()
                    return
                self._ok = True
                self._ready.set()
                lprint("剪贴板监听已启动（独立线程 + 消息泵）")
                msg = ctypes.wintypes.MSG()
                while True:
                    ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if ret == 0 or ret == -1:
                        break
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
                lprint(f"剪贴板消息泵退出（GetMessageW={ret}）")
            except Exception as e:
                lprint(f"剪贴板监听线程异常: {e}")
                self._ready.set()
            finally:
                if self._hwnd:
                    try:
                        _user32.RemoveClipboardFormatListener(self._hwnd)
                    except Exception:
                        pass
                    self._hwnd = 0
                try:
                    _user32.UnregisterClassW(_CLASS_NAME, hinst)
                except Exception:
                    pass
                self._ok = False

        # ── 消息处理 ──
        def _wnd_proc(self, hwnd, msg, wparam, lparam):
            try:
                if msg == _WM_CLIPBOARDUPDATE:
                    self._on_clipboard_update()
                    return 0
                if msg == _WM_TIMER and int(wparam) == _RETRY_TIMER_ID:
                    self._on_retry_timer()
                    return 0
                if msg == _WM_CLOSE:
                    _user32.DestroyWindow(hwnd)
                    return 0
                if msg == _WM_DESTROY:
                    _user32.PostQuitMessage(0)
                    return 0
            except Exception as e:
                lprint(f"剪贴板监听窗口过程异常: {e}")
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        def _on_clipboard_update(self) -> None:
            seq = _user32.GetClipboardSequenceNumber()
            if seq == self._last_seq:
                return
            self._last_seq = seq
            self._rec._bump("updates_total")
            status, item, png = self._read_clipboard()
            if status == "ok":
                self._retry_left = 0
                self._rec._emit(item, png)
                return
            if status == "empty":
                self._rec._bump("unknown_format")
                return
            # busy / no_data：剪贴板被占用或所有者未响应 → 挂重试链
            self._schedule_retry(seq)

        def _schedule_retry(self, seq: int) -> None:
            self._retry_seq = seq
            self._retry_left = _RETRY_MAX
            self._retry_delay = _RETRY_BASE_MS
            if not _user32.SetTimer(self._hwnd, _RETRY_TIMER_ID, self._retry_delay, None):
                self._rec._bump("read_failures", log=True)
                lprint(f"剪贴板读取失败且无法重试（seq={seq}）")

        def _on_retry_timer(self) -> None:
            _user32.KillTimer(self._hwnd, _RETRY_TIMER_ID)
            if _user32.GetClipboardSequenceNumber() != self._retry_seq:
                # 内容已被后续复制取代，旧内容不再补记
                self._rec._bump("superseded")
                self._retry_left = 0
                return
            status, item, png = self._read_clipboard()
            if status == "ok":
                self._rec._bump("retry_success")
                self._retry_left = 0
                self._rec._emit(item, png)
                return
            if status == "empty":
                self._retry_left = 0
                return
            self._retry_left -= 1
            if self._retry_left <= 0:
                self._rec._bump("read_failures", log=True)
                lprint(f"剪贴板读取失败，已重试 {_RETRY_MAX} 次（seq={self._retry_seq}）")
                return
            self._retry_delay *= 2
            _user32.SetTimer(self._hwnd, _RETRY_TIMER_ID, self._retry_delay, None)

        # ── 读取（必须在 CloseClipboard 之前把字节拷出来）──
        @staticmethod
        def _global_bytes(fmt: int) -> Optional[bytes]:
            handle = _user32.GetClipboardData(fmt)
            if not handle:
                return None
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                size = int(_kernel32.GlobalSize(handle))
                if size <= 0:
                    return None
                return ctypes.string_at(ptr, size)
            finally:
                _kernel32.GlobalUnlock(handle)

        def _read_text(self) -> str:
            raw = self._global_bytes(_CF_UNICODETEXT)
            if raw:
                try:
                    return raw.decode("utf-16-le", errors="replace").rstrip("\x00")
                except Exception:
                    return ""
            raw = self._global_bytes(_CF_TEXT)
            if raw:
                try:
                    return raw.decode("mbcs", errors="replace").rstrip("\x00")
                except Exception:
                    return ""
            return ""

        def _read_drop_files(self) -> list:
            handle = _user32.GetClipboardData(_CF_HDROP)
            if not handle:
                return []
            count = int(_shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0))
            files = []
            buf = ctypes.create_unicode_buffer(32768)
            for i in range(count):
                if _shell32.DragQueryFileW(handle, i, buf, len(buf)):
                    files.append(buf.value)
            return files

        def _read_clipboard(self) -> tuple:
            """返回 ``(status, item, png_bytes)``，status ∈ ok/empty/busy/no_data。"""
            if not _user32.OpenClipboard(self._hwnd):
                return ("busy", None, None)
            try:
                has_png = bool(
                    self._png_format
                    and _user32.IsClipboardFormatAvailable(self._png_format)
                )
                has_dibv5 = bool(_user32.IsClipboardFormatAvailable(_CF_DIBV5))
                has_dib = bool(_user32.IsClipboardFormatAvailable(_CF_DIB))
                has_files = bool(_user32.IsClipboardFormatAvailable(_CF_HDROP))
                has_text = bool(
                    _user32.IsClipboardFormatAvailable(_CF_UNICODETEXT)
                    or _user32.IsClipboardFormatAvailable(_CF_TEXT)
                )
                # 1) 图片优先（图文混排时同时保留文本）
                if has_png or has_dibv5 or has_dib:
                    text = self._read_text() if has_text else ""
                    built = None
                    if has_png:
                        raw = self._global_bytes(self._png_format)
                        if raw:
                            built = _encode_image_png(raw, text)
                        elif not (has_dibv5 or has_dib):
                            return ("no_data", None, None)
                    if built is None and (has_dibv5 or has_dib):
                        dib = self._global_bytes(
                            _CF_DIBV5 if has_dibv5 else _CF_DIB)
                        if dib:
                            image = _dib_to_qimage(dib)
                            built = _encode_image(image, text) if image else None
                            if built is None:
                                self._rec._bump("unknown_format")
                        elif not has_png:
                            return ("no_data", None, None)
                    if built is not None:
                        return ("ok", built[0], built[1])
                    if has_text and text.strip():
                        item = _text_item(text)
                        if item is not None:
                            return ("ok", item, None)
                    return ("empty", None, None)
                # 2) 文件 / 文件夹
                if has_files:
                    item = _file_item(self._read_drop_files())
                    if item is not None:
                        return ("ok", item, None)
                # 3) 文本
                if has_text:
                    text = self._read_text()
                    item = _text_item(text)
                    if item is not None:
                        return ("ok", item, None)
                    if text:
                        return ("empty", None, None)
                    return ("no_data", None, None)
                return ("empty", None, None)
            finally:
                _user32.CloseClipboard()


class _QtClipboardFallback(QtCore.QObject):
    """非 Windows 回退：``QClipboard.dataChanged`` 驱动（同一套解析与去重）。"""

    def __init__(self, recorder: ClipboardRecorder) -> None:
        super().__init__()
        self._rec = recorder

    def start(self) -> None:
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.dataChanged.connect(self._on_changed)
        lprint("剪贴板回退捕获已启动（QClipboard.dataChanged）")

    def stop(self) -> None:
        try:
            QtWidgets.QApplication.clipboard().dataChanged.disconnect(self._on_changed)
        except Exception:
            pass

    def _on_changed(self) -> None:
        try:
            item, png = self._snapshot()
        except Exception as e:
            self._rec._bump("read_failures", log=True)
            lprint(f"读取剪贴板失败: {e}")
            return
        if item is None:
            self._rec._bump("unknown_format")
            return
        self._rec._emit(item, png)

    @staticmethod
    def _snapshot() -> tuple:
        clipboard = QtWidgets.QApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            return (None, None)
        try:
            if mime.hasImage():
                image = clipboard.image()
                if not image.isNull():
                    text = clipboard.text() or ""
                    built = _encode_image(image, text)
                    if built is not None:
                        return (built[0], built[1])
            if mime.hasUrls():
                item = _file_item([u.toLocalFile() for u in mime.urls()])
                if item is not None:
                    return (item, None)
            item = _text_item(clipboard.text() or "")
            if item is not None:
                return (item, None)
        except Exception as e:
            lprint(f"解析剪贴板内容失败: {e}")
        return (None, None)


def instance() -> ClipboardRecorder:
    return ClipboardRecorder.instance()


def start(on_capture: Optional[Callable] = None) -> None:
    ClipboardRecorder.instance().start(on_capture)


def stop(timeout: float = 3.0) -> None:
    ClipboardRecorder.instance().stop(timeout)


def write_to_clipboard(
    text: Optional[str] = None,
    image: Optional[QtGui.QImage] = None,
    files: Optional[list] = None,
) -> None:
    """应用内所有写系统剪贴板操作的统一入口。"""
    ClipboardRecorder.instance().write_to_clipboard(
        text=text, image=image, files=files)


def diagnostics() -> dict:
    return ClipboardRecorder.instance().diagnostics()
