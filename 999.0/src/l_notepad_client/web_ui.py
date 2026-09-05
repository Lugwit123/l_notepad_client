# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import webbrowser

from PySide6 import QtCore, QtGui, QtWidgets

from l_qframelesswindow import L_FramelessMainWindow


class WebNotepadWindow(L_FramelessMainWindow):
    LOADING_HTML = (
        "<html><body style='margin:0;height:100vh;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;font-family:Microsoft YaHei,sans-serif;"
        "color:#555;background:#f7f7f7;'>"
        "<div style='font-size:16px'>正在启动本地服务…</div>"
        "<div style='font-size:12px;color:#999;margin-top:8px'>首次启动需初始化后端，请稍候</div>"
        "</body></html>"
    )

    def __init__(self, url: str, *, defer_load: bool = False) -> None:
        super().__init__()
        self.url = url
        self._defer_load = defer_load
        self._view: object | None = None
        self._fallback_label: QtWidgets.QLabel | None = None
        self.setWindowTitle("L Notepad - Web")
        self._settings = QtCore.QSettings("Lugwit", "l_notepad")
        self.resize(1100, 720)
        self._restore_window_state()
        
        # 设置帮助文档目录
        doc_dir = os.path.join(os.path.dirname(__file__), "doc")
        self.setHelpDocumentDir(doc_dir)

        # Try embedded web view; fall back to system browser if QtWebEngine is missing.
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView  # type: ignore
        except Exception:
            self._setup_fallback()
            if not self._defer_load:
                webbrowser.open(self.url)
            return

        view = QWebEngineView()
        self._view = view
        if self._defer_load:
            view.setHtml(self.LOADING_HTML)
        else:
            view.setUrl(QtCore.QUrl(self.url))
        self.setCentralWidget(view)

        tb = QtWidgets.QToolBar("导航")
        tb.setMovable(False)
        self.addToolBar(tb)

        act_back = QtGui.QAction("返回", self)
        act_forward = QtGui.QAction("前进", self)
        act_reload = QtGui.QAction("刷新", self)
        act_home = QtGui.QAction("主页", self)
        act_open = QtGui.QAction("外部打开当前页", self)
        act_copy = QtGui.QAction("复制当前链接", self)

        act_back.triggered.connect(view.back)
        act_forward.triggered.connect(view.forward)
        act_reload.triggered.connect(view.reload)
        act_home.triggered.connect(lambda: view.setUrl(QtCore.QUrl(self.url)))
        act_open.triggered.connect(lambda: webbrowser.open(view.url().toString() or self.url))
        act_copy.triggered.connect(lambda: QtWidgets.QApplication.clipboard().setText(view.url().toString() or self.url))

        tb.addAction(act_back)
        tb.addAction(act_forward)
        tb.addAction(act_reload)
        tb.addSeparator()
        tb.addAction(act_home)
        tb.addSeparator()
        tb.addAction(act_open)
        tb.addAction(act_copy)

        self.statusBar().showMessage(self.url)
        view.urlChanged.connect(lambda u: self.statusBar().showMessage(u.toString()))
        view.loadFinished.connect(self._on_load_finished)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        self._save_window_state()
        super().closeEvent(event)

    def load_backend(self) -> None:
        """后端就绪后加载页面（defer_load 模式由外部调用）。"""
        if self._view is not None:
            self._view.setUrl(QtCore.QUrl(self.url))
        else:
            webbrowser.open(self.url)

    def show_startup_error(self, message: str) -> None:
        if self._view is not None:
            self._view.setHtml(
                "<html><body style='margin:0;height:100vh;display:flex;"
                "align-items:center;justify-content:center;font-family:Microsoft YaHei,sans-serif;"
                "color:#a33;background:#f7f7f7;'><div style='font-size:15px;max-width:80%;"
                "text-align:center'>"
                + message.replace("&", "&amp;").replace("<", "&lt;")
                + "</div></body></html>"
            )
        elif self._fallback_label is not None:
            self._fallback_label.setText(message)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            return
        QtWidgets.QMessageBox.warning(
            self,
            "加载失败",
            "网页加载失败。\n"
            "可能原因：后端未启动 / 端口被占用 / 网络策略阻拦。\n"
            "你可以尝试「刷新」，或使用工具栏的「外部打开当前页」。",
        )

    def _restore_window_state(self) -> None:
        geo = self._settings.value("window/geometry")
        state = self._settings.value("window/state")
        if isinstance(geo, (bytes, bytearray)):
            self.restoreGeometry(geo)
        if isinstance(state, (bytes, bytearray)):
            self.restoreState(state)

    def _save_window_state(self) -> None:
        self._settings.setValue("window/geometry", self.saveGeometry())
        self._settings.setValue("window/state", self.saveState())

    def _setup_fallback(self) -> None:
        w = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(w)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        self.setCentralWidget(w)

        label = QtWidgets.QLabel(
            "当前环境缺少 QtWebEngine，无法内嵌网页。\n"
            + ("后端启动后将自动用系统默认浏览器打开网页端。"
               if self._defer_load else "已尝试用系统默认浏览器打开网页端。")
        )
        label.setWordWrap(True)
        self._fallback_label = label

        url_edit = QtWidgets.QLineEdit(self.url)
        url_edit.setReadOnly(True)

        btn_row = QtWidgets.QHBoxLayout()
        btn_open = QtWidgets.QPushButton("用浏览器打开")
        btn_copy = QtWidgets.QPushButton("复制链接")
        btn_open.clicked.connect(lambda: webbrowser.open(self.url))
        btn_copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self.url))
        btn_row.addWidget(btn_open)
        btn_row.addWidget(btn_copy)
        btn_row.addStretch(1)

        layout.addWidget(label)
        layout.addWidget(url_edit)
        layout.addLayout(btn_row)
        layout.addStretch(1)
        self.setCentralWidget(w)

