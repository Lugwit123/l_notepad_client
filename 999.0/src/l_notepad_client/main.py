# -*- coding: utf-8 -*-

from __future__ import annotations

import os

# 统一使用 pytracemp 的 lprint，并强制 logging 模式（标准日志格式：时间-级别-消息，无源码跟踪）。
# 注意：环境里 Lugwit_Debug 可能已被预置为 'true'（inspect），这里必须强制覆盖。
os.environ["Lugwit_Debug"] = "logging"

import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from pytracemp import lprint

from .api_client import NotepadApi
from .web_ui import WebNotepadWindow


def _set_windows_appid(appid: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def _acquire_single_instance_lock(app: QtWidgets.QApplication) -> QtCore.QLockFile | None:
    """带 API 模式单实例：使用 QLockFile 避免多开。"""
    data_dir_str = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.AppDataLocation
    )
    base_dir = Path(data_dir_str or Path.home())
    lock_path = base_dir / "l_notepad_with_api.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock = QtCore.QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QtWidgets.QMessageBox.warning(
            None,
            "L Notepad 已在运行",
            "L Notepad（带 API 模式）已经有一个实例在运行，禁止多开。",
        )
        return None
    return lock


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def _is_backend_up(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """非阻塞 TCP 探测：uvicorn 在 create_app（含 DB 初始化/迁移）完成后才 bind，
    端口可连即代表后端就绪。用于 GUI 侧 QTimer 轮询，避免卡住界面。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect((host, port))
        return True
    except OSError:
        return False


_BACKEND_READY_DEADLINE_S = 15.0


def _set_webengine_profile_dir() -> None:
    """固定 QtWebEngine 的 Chromium user-data-dir，避免默认临时目录策略导致的
    冷启动反复初始化缓存/编译着色器，加快 WebView 首开速度。"""
    from pathlib import Path as _Path

    profile_dir = _Path.home() / ".Lugwit" / "l_notepad" / "webview_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--user-data-dir" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{flags} --user-data-dir={profile_dir}".strip()
        )


def _start_backend_subprocess(host: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["L_NOTEPAD_HOST"] = host
    env["L_NOTEPAD_PORT"] = str(port)
    cmd = [sys.executable, "-m", "l_notepad_server.backend_server", "--host", host, "--port", str(port)]
    return subprocess.Popen(cmd, env=env)


def _watch_backend_ready(
    win: WebNotepadWindow,
    backend: subprocess.Popen,
    host: str,
    port: int,
    deadline: float,
) -> None:
    """用 QTimer 轮询后端就绪状态，不阻塞 UI；就绪后加载页面。"""
    if _is_backend_up(host, port, timeout_s=0.3):
        win.load_backend()
        return
    if backend.poll() is not None:
        win.show_startup_error(
            f"后端服务启动失败（退出码 {backend.returncode}），"
            "请查看日志或重新打开窗口。"
        )
        return
    if time.monotonic() >= deadline:
        win.show_startup_error(
            f"后端服务在 {int(_BACKEND_READY_DEADLINE_S)} 秒内未就绪，"
            "可点击工具栏「刷新」重试。"
        )
        return
    QtCore.QTimer.singleShot(
        200, lambda: _watch_backend_ready(win, backend, host, port, deadline)
    )


def main() -> int:
    _set_windows_appid("Lugwit.l_notepad.with_api")
    host = os.environ.get("L_NOTEPAD_HOST", "127.0.0.1")
    port_env = int(os.environ.get("L_NOTEPAD_PORT", "8765"))
    port = _find_free_port() if port_env == 0 else port_env
    if port_env != 0 and not _is_port_available(host, port):
        lprint(f"[l_notepad] ERROR: 端口 {port} 被占用，请先关闭占用者（或设置 L_NOTEPAD_PORT=0 使用随机端口）。", level="ERROR")
        return 3
    base_url = f"http://{host}:{port}"
    web_url = f"{base_url}/web"

    backend = _start_backend_subprocess(host, port)

    _set_webengine_profile_dir()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    try:
        from l_qt_wgt_lib import install_combobox_wheel_guard

        install_combobox_wheel_guard(app)
    except Exception:
        pass
    lock = _acquire_single_instance_lock(app)
    if lock is None:
        with suppress(Exception):
            backend.terminate()
        return 1
    icon_path = Path(__file__).resolve().parent / "static" / "favicon.svg"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))
    # Desktop app loads the web frontend directly.
    _ = NotepadApi(base_url)  # keep for potential future health/extension
    # 先出窗口（显示启动中页面），后端就绪后再加载，避免启动期长时间白屏/无响应。
    win = WebNotepadWindow(web_url, defer_load=True)
    win.show()
    QtCore.QTimer.singleShot(
        50,
        lambda: _watch_backend_ready(
            win, backend, host, port, time.monotonic() + _BACKEND_READY_DEADLINE_S
        ),
    )
    code = app.exec()

    with suppress(Exception):
        backend.terminate()
    with suppress(Exception):
        backend.wait(timeout=2)
    if lock is not None:
        lock.unlock()
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())

