# -*- coding: utf-8 -*-
"""
账号收藏标签页 - 收藏常用账号信息
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict

from PySide6 import QtCore, QtGui, QtWidgets

from pytracemp import lprint

# DPAPI 统一走 lugwit_auth_client 的唯一实现（避免多份 DATA_BLOB 抢 crypt32 全局 argtypes）
# 历史实现是 l_qframelesswindow.dpapi，已随该包的 `_auth_client/` 内嵌副本一起收敛到客户端包。
from lugwit_auth_client.dpapi import (
    dpapi_available as _dpapi_available,
    protect as _dpapi_protect,
    unprotect as _dpapi_unprotect,
)

from . import clipboard_recorder
from . import fav_vars
from .folder_favorites_widget import (
    attach_var_preview,
    _favorites_copy_to_clipboard,
    _favorites_read_from_clipboard,
    _icon_with_cloud,
)


def _account_icon() -> QtGui.QIcon:
    """绘制一个简单的「人形」图标，作为账号收藏条目的基础类型图标。"""
    pm = QtGui.QPixmap(16, 16)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("#8ab4f8"))
    painter.drawEllipse(4, 1, 8, 8)  # 头部
    painter.drawRoundedRect(1, 10, 14, 6, 3, 3)  # 肩部
    painter.end()
    return QtGui.QIcon(pm)


class AccountItem(TypedDict, total=False):
    """账号收藏项目类型定义"""
    id: int  # PostgreSQL 主键（本地 JSON 数据无此字段）
    name: str  # 显示名称
    username: str  # 用户名
    password: str  # 密码
    server: str  # 服务器地址
    notes: str  # 备注
    custom_fields: dict[str, str]  # 自定义字段（全局字段名 -> 值）
    cloud: bool  # 是否已上传到服务器（True=云项，缺省=本地项）


def _dpapi_encrypt(data: bytes) -> bytes:
    """本地离线账号缓存加密（Windows DPAPI；非 Windows 退化为 base64）。"""
    if not _dpapi_available():
        import base64
        return base64.b64encode(data)
    return _dpapi_protect(data)


def _dpapi_decrypt(data: bytes) -> bytes:
    """本地离线账号缓存解密，与 _dpapi_encrypt 对应。"""
    if not _dpapi_available():
        import base64
        return base64.b64decode(data)
    return _dpapi_unprotect(data)


class CustomFieldManageDialog(QtWidgets.QDialog):
    """管理全局自定义字段名的对话框（增删改）"""

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        field_names: list[str],
    ) -> None:
        super().__init__(parent)
        self._field_names: list[str] = list(field_names)
        self._row_widgets: list[tuple[QtWidgets.QLineEdit, QtWidgets.QPushButton]] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setWindowTitle("管理自定义字段")
        self.setMinimumWidth(400)

        layout = QtWidgets.QVBoxLayout(self)

        tip = QtWidgets.QLabel("此处定义的自定义字段将应用到所有账号。每个账号可填入不同的值。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #888; font-size: 11px; padding: 4px 0;")
        layout.addWidget(tip)

        # 字段名容器
        self._fields_layout = QtWidgets.QVBoxLayout()
        self._fields_layout.setSpacing(4)
        layout.addLayout(self._fields_layout)

        # 填充已有字段
        for name in self._field_names:
            self._add_field_row(name)

        # 添加按钮
        add_btn = QtWidgets.QPushButton("+ 添加字段")
        add_btn.setFixedWidth(120)
        add_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(lambda: self._add_field_row(""))
        add_btn_layout = QtWidgets.QHBoxLayout()
        add_btn_layout.addWidget(add_btn)
        add_btn_layout.addStretch()
        layout.addLayout(add_btn_layout)

        layout.addStretch()

        # 确认/取消
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _add_field_row(self, name: str = "") -> None:
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)

        name_input = QtWidgets.QLineEdit(name)
        name_input.setPlaceholderText("字段名（如：部门、工号）")

        del_btn = QtWidgets.QPushButton()
        del_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon))
        del_btn.setFixedSize(28, 24)
        del_btn.setToolTip("删除此字段")
        del_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(
            "QPushButton { color: #e05555; border: 1px solid transparent; "
            "border-radius: 3px; background: transparent; }"
            "QPushButton:hover { border-color: #e05555; background: rgba(224,85,85,0.15); }"
        )
        del_btn.clicked.connect(lambda: self._remove_field_row(row, name_input, del_btn))

        row.addWidget(name_input)
        row.addWidget(del_btn)
        self._fields_layout.addLayout(row)
        self._row_widgets.append((name_input, del_btn))

    def _remove_field_row(
        self,
        row: QtWidgets.QHBoxLayout,
        name_input: QtWidgets.QLineEdit,
        del_btn: QtWidgets.QPushButton,
    ) -> None:
        for i in reversed(range(row.count())):
            item = row.itemAt(i)
            if item.widget():
                item.widget().deleteLater()
        self._fields_layout.removeItem(row)
        self._row_widgets = [
            (n, b) for (n, b) in self._row_widgets if n is not name_input
        ]

    def get_field_names(self) -> list[str]:
        """获取最终字段名列表（去重去空）"""
        names: list[str] = []
        seen: set[str] = set()
        for name_input, _ in self._row_widgets:
            n = name_input.text().strip()
            if n and n not in seen:
                names.append(n)
                seen.add(n)
        return names


class AddAccountDialog(QtWidgets.QDialog):
    """添加/编辑账号的对话框"""

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        name: str = "",
        username: str = "",
        password: str = "",
        server: str = "",
        notes: str = "",
        custom_fields: dict[str, str] | None = None,
        field_names: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._custom_field_inputs: dict[str, QtWidgets.QLineEdit] = {}
        self._setup_ui(name, username, password, server, notes, custom_fields or {}, field_names or [])

    def _setup_ui(
        self,
        name: str,
        username: str,
        password: str,
        server: str,
        notes: str,
        custom_fields: dict[str, str],
        field_names: list[str],
    ) -> None:
        """初始化 UI"""
        self.setWindowTitle("添加账号" if not name else "编辑账号")
        self.setMinimumWidth(500)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)

        # 名称输入
        name_layout = QtWidgets.QHBoxLayout()
        name_label = QtWidgets.QLabel("显示名称:")
        name_label.setFixedWidth(80)
        self.name_input = QtWidgets.QLineEdit(name)
        self.name_input.setPlaceholderText("例如：公司服务器账号")
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)

        # 用户名输入
        username_layout = QtWidgets.QHBoxLayout()
        username_label = QtWidgets.QLabel("用户名:")
        username_label.setFixedWidth(80)
        self.username_input = QtWidgets.QLineEdit(username)
        self.username_input.setPlaceholderText("输入用户名")
        username_layout.addWidget(username_label)
        username_layout.addWidget(self.username_input)
        layout.addLayout(username_layout)

        # 密码输入
        password_layout = QtWidgets.QHBoxLayout()
        password_label = QtWidgets.QLabel("密码:")
        password_label.setFixedWidth(80)
        self.password_input = QtWidgets.QLineEdit(password)
        self.password_input.setEchoMode(QtWidgets.QLineEdit.Password)
        self.password_input.setPlaceholderText("输入密码")
        show_password_btn = QtWidgets.QPushButton("显示")
        show_password_btn.setFixedWidth(50)
        show_password_btn.setCheckable(True)
        show_password_btn.toggled.connect(
            lambda checked: self.password_input.setEchoMode(
                QtWidgets.QLineEdit.Normal if checked else QtWidgets.QLineEdit.Password
            )
        )
        password_layout.addWidget(password_label)
        password_layout.addWidget(self.password_input)
        password_layout.addWidget(show_password_btn)
        layout.addLayout(password_layout)

        # 服务器地址输入
        server_layout = QtWidgets.QHBoxLayout()
        server_label = QtWidgets.QLabel("服务器地址:")
        server_label.setFixedWidth(80)
        self.server_input = QtWidgets.QLineEdit(server)
        self.server_input.setPlaceholderText("例如：192.168.1.100 或 server.example.com")
        server_layout.addWidget(server_label)
        server_layout.addWidget(self.server_input)
        layout.addLayout(server_layout)

        # 备注输入
        notes_layout = QtWidgets.QHBoxLayout()
        notes_label = QtWidgets.QLabel("备注:")
        notes_label.setFixedWidth(80)
        self.notes_input = QtWidgets.QLineEdit(notes)
        self.notes_input.setPlaceholderText("可选的备注信息")
        notes_layout.addWidget(notes_label)
        notes_layout.addWidget(self.notes_input)
        layout.addLayout(notes_layout)

        # ── 全局自定义字段（字段名只读，值可填） ──
        if field_names:
            separator = QtWidgets.QFrame()
            separator.setFrameShape(QtWidgets.QFrame.HLine)
            separator.setStyleSheet("QFrame { color: #444; margin: 4px 0; }")
            layout.addWidget(separator)

            custom_header = QtWidgets.QHBoxLayout()
            custom_label = QtWidgets.QLabel("自定义字段:")
            custom_label.setFixedWidth(80)
            custom_header.addWidget(custom_label)
            custom_header.addStretch()
            layout.addLayout(custom_header)

            for field_name in field_names:
                row = QtWidgets.QHBoxLayout()
                row.setSpacing(4)

                # 字段名标签（只读，斜体显示以示区别）
                fn_label = QtWidgets.QLabel(f"{field_name}:")
                fn_label.setFixedWidth(130)
                fn_label.setStyleSheet("color: #89DDFF; font-style: italic;")

                value_input = QtWidgets.QLineEdit(custom_fields.get(field_name, ""))
                value_input.setPlaceholderText(f"输入{field_name}")

                row.addWidget(fn_label)
                row.addWidget(value_input)
                row.addStretch()
                layout.addLayout(row)

                self._custom_field_inputs[field_name] = value_input

        # 内置变量实时预览（{y}{m}{d} {pc} {user} ...）
        preview_pairs = [
            ("名称", self.name_input),
            ("用户名", self.username_input),
            ("服务器", self.server_input),
            ("备注", self.notes_input),
        ] + [(key, input_widget) for key, input_widget in self._custom_field_inputs.items()]
        attach_var_preview(layout, preview_pairs)

        layout.addStretch()

        # 按钮
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        scroll.setWidget(content)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def get_account_data(self) -> AccountItem:
        """获取账号数据"""
        custom_fields: dict[str, str] = {}
        for field_name, input_widget in self._custom_field_inputs.items():
            v = input_widget.text().strip()
            if v:
                custom_fields[field_name] = v

        return AccountItem(
            name=self.name_input.text().strip(),
            username=self.username_input.text().strip(),
            password=self.password_input.text(),
            server=self.server_input.text().strip(),
            notes=self.notes_input.text().strip(),
            custom_fields=custom_fields,
        )


class AccountFavoritesWidget(QtWidgets.QWidget):
    """账号收藏桌面组件（嵌入到 l_notepad）"""

    # 登录凭据失效（token 过期/被拒绝），通知主窗口清除登录状态
    login_expired = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("tab_account_favorites")
        self._accounts: list[AccountItem] = []
        # 列表行号 → 账号 dict（真实对象）映射，随 _refresh_list 重建。
        # 未登录时云账号会被隐藏，导致 list_widget 行号与 self._accounts 下标
        # 不一致；且 item.data(UserRole) 返回的是 QVariant 深拷贝副本，无法按
        # 对象身份定位回 self._accounts。此映射按“可见顺序”记录真实对象。
        self._row_to_account: dict[int, AccountItem] = {}
        self._custom_field_names: list[str] = []  # 全局自定义字段名模板
        self._ui_finalized = False
        self.api = None  # NotepadApi（由主窗口注入；为 None 时回退本地 JSON）
        self._source = "local"  # 数据源: "db"(PostgreSQL) / "local"(本地 JSON)
        self._cloud_ready_state = False  # 是否已成功从服务器加载云账号（控制云 item 显示）
        self._setup_data()
        QtCore.QTimer.singleShot(0, self.finalize_ui)

    def finalize_ui(self) -> None:
        """延迟初始化 UI 和列表（.ui 子控件已就绪）"""
        if self._ui_finalized:
            return
        self._ui_finalized = True
        self._setup_ui()
        self._refresh_list()

    def _setup_data(self) -> None:
        """初始化数据（从文件加载）"""
        from .paths import data_root

        base = data_root()
        self._favorites_file = base / "account_favorites.json"
        self._custom_fields_file = base / "account_custom_fields.json"
        self._load_favorites()
        self._load_custom_field_names()

    def _setup_ui(self) -> None:
        """初始化 UI。优先复用 main_window.ui 中定义的控件。"""
        self.list_widget = self.findChild(QtWidgets.QListWidget, "account_favorites_list")
        add_btn = self.findChild(QtWidgets.QPushButton, "btn_add_account")
        copy_btn = self.findChild(QtWidgets.QPushButton, "btn_copy_account")
        edit_btn = self.findChild(QtWidgets.QPushButton, "btn_edit_account")
        delete_btn = self.findChild(QtWidgets.QPushButton, "btn_delete_account")
        custom_fields_btn = self.findChild(QtWidgets.QPushButton, "btn_custom_fields")
        self.hint_label = self.findChild(QtWidgets.QLabel, "account_favorites_hint_label")

        if self.list_widget is not None:
            self._apply_list_style()
            self.list_widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
            self.list_widget.itemDoubleClicked.connect(self._copy_account_info)
            if add_btn:
                add_btn.clicked.connect(self._add_account)
            if copy_btn:
                copy_btn.clicked.connect(self._copy_account_info)
            if edit_btn:
                edit_btn.clicked.connect(self._edit_account)
            if delete_btn:
                delete_btn.clicked.connect(self._remove_account)
            if custom_fields_btn:
                custom_fields_btn.clicked.connect(self._manage_custom_fields)
            return

        # 回退：无 .ui 时自行创建
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        title_label = QtWidgets.QLabel("👤 账号收藏 (account_favorites.json)")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; color: #2c3e50;")
        main_layout.addWidget(title_label)

        btn_layout = QtWidgets.QHBoxLayout()
        add_btn_fb = QtWidgets.QPushButton(" 添加账号")
        add_btn_fb.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder))
        add_btn_fb.clicked.connect(self._add_account)
        btn_layout.addWidget(add_btn_fb)

        copy_btn_fb = QtWidgets.QPushButton(" 复制信息")
        copy_btn_fb.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton))
        copy_btn_fb.clicked.connect(self._copy_account_info)
        btn_layout.addWidget(copy_btn_fb)

        edit_btn_fb = QtWidgets.QPushButton(" 编辑")
        edit_btn_fb.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogDetailedView))
        edit_btn_fb.clicked.connect(self._edit_account)
        btn_layout.addWidget(edit_btn_fb)

        delete_btn_fb = QtWidgets.QPushButton(" 删除")
        delete_btn_fb.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogCancelButton))
        delete_btn_fb.clicked.connect(self._remove_account)
        btn_layout.addWidget(delete_btn_fb)

        custom_fields_btn_fb = QtWidgets.QPushButton("+ 自定义字段")
        custom_fields_btn_fb.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView))
        custom_fields_btn_fb.clicked.connect(self._manage_custom_fields)
        btn_layout.addWidget(custom_fields_btn_fb)

        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setObjectName("account_favorites_list")
        self.list_widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.itemDoubleClicked.connect(self._copy_account_info)
        self._apply_list_style()
        main_layout.addWidget(self.list_widget)

        self.hint_label = QtWidgets.QLabel(
            "💡 提示：点击「添加账号」保存常用账号信息，双击或点击「复制信息」可快速复制到剪贴板"
        )
        self.hint_label.setStyleSheet("color: #7f8c8d; font-size: 12px; padding: 4px;")
        self.hint_label.setWordWrap(True)
        main_layout.addWidget(self.hint_label)

    def _apply_list_style(self) -> None:
        """应用列表样式"""
        self.list_widget.setSpacing(0)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setIconSize(QtCore.QSize(16, 16))
        self.list_widget.setStyleSheet(
            """
            QListWidget#account_favorites_list {
                padding: 2px;
                outline: none;
                border: 1px solid #404040;
                border-radius: 4px;
                
            }
            QListWidget#account_favorites_list::item {
                min-height: 22px;
                padding: 0px 1px;
                margin: 0;
                border-radius: 4px;
                color: #e0e0e0;
                font-size: 18px;
            }
            QListWidget#account_favorites_list::item:selected {
                border: 1px solid #44a8eb;
                border-radius: 4px;
                background-color: transparent;
            }
            QListWidget#account_favorites_list::item:hover {
                background-color: #2d2d2d;
            }
            """
        )

    # ── 全局自定义字段名 加载/保存 ──
    def _load_custom_field_names(self) -> None:
        """加载全局自定义字段名（优先 PostgreSQL，失败回退本地）"""
        if self._try_load_custom_fields_from_api():
            return
        self._load_custom_field_names_from_json()

    def _try_load_custom_fields_from_api(self) -> bool:
        """尝试从 PostgreSQL 加载自定义字段名（须登录授权）"""
        # 本地 api（LocalNotepadApi）无账号能力，直接回退本地，不打印误导错误
        if not hasattr(self.api, "get_account_custom_fields"):
            return False
        try:
            if not self._ensure_api_token():
                return False
            names = self.api.get_account_custom_fields()
            self._custom_field_names = [str(n) for n in names if str(n).strip()]
            return True
        except Exception as e:
            lprint(f"从 PostgreSQL 加载自定义字段失败，回退本地: {e}")
            return False

    def _load_custom_field_names_from_json(self) -> None:
        """从本地 JSON 文件加载全局自定义字段名"""
        try:
            if self._custom_fields_file.exists():
                with open(self._custom_fields_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._custom_field_names = [str(n) for n in data if str(n).strip()]
        except Exception as e:
            lprint(f"加载自定义字段名失败: {e}")
            self._custom_field_names = []

    def _save_custom_field_names(self) -> None:
        """保存全局自定义字段名到文件"""
        try:
            with open(self._custom_fields_file, "w", encoding="utf-8") as f:
                json.dump(self._custom_field_names, f, ensure_ascii=False, indent=2)
        except Exception as e:
            lprint(f"保存自定义字段名失败: {e}")

    def _manage_custom_fields(self) -> None:
        """打开管理全局自定义字段的对话框"""
        dialog = CustomFieldManageDialog(self, self._custom_field_names)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            self._custom_field_names = dialog.get_field_names()
            self._save_custom_field_names()
            if self._cloud_ready():
                try:
                    self.api.save_account_custom_fields(self._custom_field_names)
                except Exception as e:
                    lprint(f"保存自定义字段到服务器失败: {e}")
            self._refresh_list()

    # ── 账号数据 加载/保存 ──
    def reload_data(self) -> None:
        """（供外部注入 api 后调用）重新加载数据（云 + 本地合并显示）"""
        self._load_custom_field_names()
        self._try_load_from_api()
        self._refresh_list()

    def set_cloud_api(self, api) -> None:
        """设置云同步 API（与文件夹/命令/网址收藏标签统一入口）。"""
        self.api = api
        self._load_custom_field_names()
        self.reload_data()

    def _ensure_api_token(self) -> bool:
        """确保后端 API 携带登录凭据；拿不到就返回 False（上层回退本地数据）。

        **不再自己去取 token**：`/api/v1/auth/auto` 回环授权已按 P0 默认关闭，
        未登录就是未登录。唯一例外是环境变量 `LUGWIT_ACCESS_TOKEN`（脚本/CI 场景），
        交互式使用请在标题栏登录（登录后 `api.token` 已就位，第 613 行就返回 True）。
        """
        if self.api is None:
            return False
        if getattr(self.api, "token", None):
            return True
        env_token = (os.environ.get("LUGWIT_ACCESS_TOKEN") or "").strip()
        if env_token:
            self.api.token = env_token
            return True
        lprint(
            "未登录：云账号数据不可用（请先在标题栏登录；脚本场景可设 "
            "LUGWIT_ACCESS_TOKEN）"
        )
        return False

    def _try_load_from_api(self) -> bool:
        """尝试从服务器加载云账号并与本地账号合并；成功返回 True"""
        # 本地 api（LocalNotepadApi）无账号能力，直接回退本地，不打印误导错误
        if not hasattr(self.api, "list_accounts"):
            self._cloud_ready_state = False
            self._load_from_json()
            self._source = "local"
            return False
        try:
            if not self._ensure_api_token():
                self._cloud_ready_state = False
                return False
            data = self.api.list_accounts()
            local_items = [a for a in self._accounts if not a.get("cloud") and not a.get("id")]
            accounts = []
            for item in data:
                custom_fields = item.get("custom_fields") or {}
                accounts.append(AccountItem(
                    **{**item, "custom_fields": custom_fields, "cloud": True}))
            self._accounts = accounts + local_items
            self._source = "db"
            self._cloud_ready_state = True
            # 注意：不在这里写本地缓存——只有「登录用户」加载成功才写（见
            # _apply_login 显式 _save_offline_cache 与增删改后的同步），
            # 避免登出后本机免登录(system01)的空列表覆盖登录用户缓存。
            return True
        except Exception as e:
            if "401" in str(e):
                # 登录凭据失效：清 token、回退未登录态，并通知主窗口
                if self.api is not None:
                    self.api.token = None
                self._cloud_ready_state = False
                self._source = "local"
                self.login_expired.emit()
                return False
            lprint(f"从服务器加载账号失败，回退本地: {e}")
            self._cloud_ready_state = False
            # 后端不可达：保留本地/上次缓存的账号副本
            self._load_from_json()
            self._source = "local"
            return False

    def _load_favorites(self) -> None:
        """从本地加密文件加载收藏（云 item 在登录后由 _try_load_from_api 合并）。"""
        self._load_from_json()

    def _load_from_json(self) -> None:
        """从本地加密文件加载账号（DPAPI 加密，兼容旧明文 JSON 与旧离线缓存 bin）。"""
        self._accounts = []
        self._source = "local"
        path = self._favorites_file
        # 升级迁移：旧版本账号只存 account_offline_cache.bin（云端快照），无 account_favorites.json
        legacy_bin = self._favorites_file.parent / "account_offline_cache.bin"
        from_legacy_bin = False
        if not path.exists() and legacy_bin.exists():
            path = legacy_bin
            from_legacy_bin = True
        try:
            if not path.exists():
                return
            raw = path.read_bytes()
            data = None
            # 新格式：整文件 DPAPI 加密；旧格式：明文 JSON（读取后自动迁移为加密）
            try:
                data = json.loads(_dpapi_decrypt(raw).decode("utf-8"))
            except Exception:
                data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, list):
                return
            accounts = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                account = AccountItem(
                    **{**item, "custom_fields": item.get("custom_fields") or {}})
                # 旧 bin 是登录时保存的云端快照（带 id 无 cloud 标记），补记为云项
                if from_legacy_bin and account.get("id"):
                    account["cloud"] = True
                accounts.append(account)
            self._accounts = accounts
        except Exception as e:
            lprint(f"加载本地账号收藏失败: {e}")
            self._accounts = []

    def _save_favorites(self) -> None:
        """保存「本地项」到本地加密文件。

        只落**本地项**（未上云、无服务端 id）：云项是服务器数据，登录时从服务端取、
        登出时本就隐藏，本地不再重复存一份密文——避免同一秘密在服务端与客户端被
        两套加密体系各保护一遍，也减少本地明文密码的暴露面。
        """
        local_only = [a for a in self._accounts if not a.get("cloud") and not a.get("id")]
        try:
            raw = json.dumps(local_only, ensure_ascii=False).encode("utf-8")
            self._favorites_file.write_bytes(_dpapi_encrypt(raw))
        except Exception as e:
            lprint(f"保存本地账号收藏失败: {e}")

    # ── 离线缓存（与本地收藏共用同一 DPAPI 加密文件）──
    def _offline_cache_path(self) -> Path:
        return self._favorites_file

    def _save_offline_cache(self) -> None:
        """把本地项加密备份到本地（云项不入本地缓存，见 `_save_favorites`）。"""
        self._save_favorites()

    def _load_offline_cache(self) -> bool:
        """从本地加密文件读取账号（离线时展示）；成功返回 True。"""
        self._load_from_json()
        return bool(self._accounts)

    # ── 云同步（本地 ↔ 服务器）──
    def _on_auth_401(self, title: str) -> None:
        """登录凭据失效：通知主窗口清除登录状态并提示重新登录"""
        self.api.token = None if self.api is not None else None
        self._source = "local"
        self._cloud_ready_state = False
        QtWidgets.QMessageBox.warning(
            self, title, "登录已过期，请点击标题栏「登录」按钮重新登录后再操作。"
        )
        self.login_expired.emit()

    def _cloud_ready(self) -> bool:
        """是否具备云同步能力（账号 API 可用且已登录/本机免登录）。"""
        if self.api is None or not hasattr(self.api, "list_accounts"):
            return False
        if getattr(self.api, "token", None):
            return True
        return self._ensure_api_token()

    def _toggle_cloud(self, account: dict) -> None:
        """右键切换：本地 ↔ 云同步。"""
        if not isinstance(account, dict):
            return
        if not self._cloud_ready():
            QtWidgets.QMessageBox.information(self, "提示", "请先登录后再使用云同步")
            return
        try:
            if account.get("cloud"):
                if account.get("id"):
                    self.api.delete_account(account["id"])
                account["cloud"] = False
                account.pop("id", None)
            else:
                created = self.api.add_account(account)
                if created and created.get("id"):
                    account["id"] = created["id"]
                account["cloud"] = True
        except Exception as exc:
            if "401" in str(exc):
                self._on_auth_401("云同步失败")
                return
            lprint(f"[账号云同步] 切换失败: {exc}")
            QtWidgets.QMessageBox.warning(self, "云同步失败", str(exc))
            return
        self._save_favorites()
        self._refresh_list()

    def _sync_all_local_to_cloud(self) -> None:
        """一次性把当前所有「本地项」上传到服务器（云同步）。"""
        if not self._cloud_ready():
            QtWidgets.QMessageBox.information(self, "提示", "请先登录后再使用云同步")
            return
        local_items = [a for a in self._accounts if not a.get("cloud")]
        if not local_items:
            QtWidgets.QMessageBox.information(self, "提示", "没有需要同步的本地账号（均已在云端）")
            return
        reply = QtWidgets.QMessageBox.question(
            self, "同步到数据库",
            f"将把 {len(local_items)} 个本地账号上传到数据库并转为云同步项，确定继续？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        ok = 0
        failed = 0
        first_error = ""
        for account in local_items:
            try:
                created = self.api.add_account(account)
                if created and created.get("id"):
                    account["id"] = created["id"]
                account["cloud"] = True
                ok += 1
            except Exception as exc:
                failed += 1
                if not first_error:
                    first_error = str(exc)
                lprint(f"[账号云同步] 上传失败: {exc}")
        self._save_favorites()
        self._refresh_list()
        if failed == 0:
            QtWidgets.QMessageBox.information(self, "同步完成", f"已成功同步 {ok} 个账号到数据库")
        else:
            QtWidgets.QMessageBox.warning(
                self, "同步完成（部分失败）",
                f"成功 {ok} 个，失败 {failed} 个。\n首个错误：{first_error}",
            )

    def _refresh_list(self) -> None:
        """刷新列表显示"""
        # finalize_ui（延迟初始化）前 list_widget 尚未就绪，跳过刷新
        if not getattr(self, "list_widget", None):
            return
        self.list_widget.clear()
        self._row_to_account = {}
        cloud_visible = getattr(self, "_cloud_ready_state", False)
        cloud_count = sum(1 for a in self._accounts if a.get("cloud"))
        local_count = len(self._accounts) - cloud_count
        if getattr(self, "hint_label", None) is not None:
            if cloud_visible and cloud_count:
                src_text = f"云 {cloud_count} · 本地 {local_count}"
            else:
                src_text = f"本地 {local_count}"
            self.hint_label.setText(
                f"💡 数据源: {src_text}｜点击「添加账号」保存常用账号信息，双击或点击「复制信息」可快速复制到剪贴板"
            )

        if not self._accounts:
            hint_text = "暂无收藏的账号，点击「添加账号」开始使用（未登录时保存在本地，登录后可在右键菜单同步到云）"
            hint_item = QtWidgets.QListWidgetItem(hint_text)
            hint_item.setForeground(QtGui.QColor("#95a5a6"))
            hint_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.list_widget.addItem(hint_item)
            return

        account_icon = _account_icon()
        for account in self._accounts:
            # 云 item 未登录时隐藏（数据在服务器，仅登录用户可见）
            if account.get("cloud") and not cloud_visible:
                continue
            name = fav_vars.expand(account.get("name", "未命名"))
            username = fav_vars.expand(account.get("username", ""))
            server = fav_vars.expand(account.get("server", ""))
            custom_fields = account.get("custom_fields", {})

            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(QtCore.QSize(0, 22))

            display_text = name
            if username:
                display_text += f" ({username})"
            if server:
                display_text += f" - {server}"
            # 显示有值的自定义字段数量
            filled = sum(1 for v in custom_fields.values() if v)
            if filled:
                display_text += f"  [+{filled}自定义]"
            item.setText(display_text)

            item.setIcon(_icon_with_cloud(account_icon, bool(account.get("cloud"))))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, account)
            self.list_widget.addItem(item)
            self._row_to_account[self.list_widget.count() - 1] = account

    def _add_account(self) -> None:
        """添加新账号（默认保存为本地项，登录后可在右键菜单同步到云）"""
        dialog = AddAccountDialog(self, field_names=self._custom_field_names)
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            account_data = dialog.get_account_data()
            if not account_data.get("name"):
                QtWidgets.QMessageBox.warning(self, "提示", "请输入显示名称")
                return
            self._accounts.append(account_data)
            self._save_favorites()
            self._refresh_list()

    def _current_account(self) -> dict | None:
        """当前列表选中项的账号 dict（真实对象）。

        用渲染时记录的「行号 → 真实对象」映射，避免未登录时云账号被隐藏导致
        行号与 self._accounts 下标错位，也避免 item.data(UserRole) 的深拷贝副本。
        """
        account = self._row_to_account.get(self.list_widget.currentRow())
        return account if isinstance(account, dict) else None

    def _find_account_index(self, account: dict) -> int:
        """按对象身份在 self._accounts 中定位下标（返回 -1 表示不存在）。"""
        for i, a in enumerate(self._accounts):
            if a is account:
                return i
        return -1

    def _edit_account(self) -> None:
        """编辑选中的账号"""
        current_item = self.list_widget.currentItem()
        if not current_item:
            QtWidgets.QMessageBox.information(self, "提示", "请先选择要编辑的账号")
            return

        account = self._current_account()
        if not account:
            return

        dialog = AddAccountDialog(
            self,
            name=account.get("name", ""),
            username=account.get("username", ""),
            password=account.get("password", ""),
            server=account.get("server", ""),
            notes=account.get("notes", ""),
            custom_fields=account.get("custom_fields", {}),
            field_names=self._custom_field_names,
        )

        if dialog.exec() == QtWidgets.QDialog.Accepted:
            new_data = dialog.get_account_data()
            if not new_data.get("name"):
                QtWidgets.QMessageBox.warning(self, "提示", "请输入显示名称")
                return
            idx = self._find_account_index(account)
            if 0 <= idx < len(self._accounts):
                old = self._accounts[idx]
                if old.get("cloud"):
                    new_data["cloud"] = True
                    new_data["id"] = old.get("id")
                self._accounts[idx] = new_data
                if new_data.get("cloud") and self._cloud_ready():
                    try:
                        if new_data.get("id"):
                            self.api.update_account(new_data["id"], new_data)
                        else:
                            created = self.api.add_account(new_data)
                            if created and created.get("id"):
                                new_data["id"] = created["id"]
                    except Exception as e:
                        if "401" in str(e):
                            self._on_auth_401("保存失败")
                            return
                        lprint(f"更新服务器账号失败: {e}")
                        QtWidgets.QMessageBox.warning(self, "保存失败", f"更新数据库失败: {e}")
                self._save_favorites()
                self._refresh_list()

    def _remove_account(self) -> None:
        """删除选中的账号"""
        current_item = self.list_widget.currentItem()
        if not current_item:
            QtWidgets.QMessageBox.information(self, "提示", "请先选择要删除的账号")
            return

        account = self._current_account()
        if not account:
            return

        name = account.get("name", "未命名")
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除账号「{name}」吗？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            idx = self._find_account_index(account)
            if 0 <= idx < len(self._accounts):
                removed = self._accounts.pop(idx)
                if removed.get("cloud") and self._cloud_ready():
                    try:
                        if removed.get("id"):
                            self.api.delete_account(removed["id"])
                    except Exception as e:
                        if "401" in str(e):
                            self._on_auth_401("删除失败")
                            return
                        lprint(f"删除服务器账号失败: {e}")
                        QtWidgets.QMessageBox.warning(self, "删除失败", f"删除数据库账号失败: {e}")
                self._save_favorites()
                self._refresh_list()

    def _copy_account_info(self) -> None:
        """复制账号信息到剪贴板"""
        current_item = self.list_widget.currentItem()
        if not current_item:
            QtWidgets.QMessageBox.information(self, "提示", "请先选择要复制的账号")
            return

        account = current_item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not account:
            return

        lines = []
        if account.get("name"):
            lines.append(f"名称: {fav_vars.expand(account['name'])}")
        if account.get("username"):
            lines.append(f"用户名: {fav_vars.expand(account['username'])}")
        if account.get("password"):
            lines.append(f"密码: {account['password']}")
        if account.get("server"):
            lines.append(f"服务器: {fav_vars.expand(account['server'])}")
        if account.get("notes"):
            lines.append(f"备注: {fav_vars.expand(account['notes'])}")
        # 自定义字段
        custom_fields = account.get("custom_fields", {})
        if custom_fields:
            for key, value in custom_fields.items():
                if value:
                    lines.append(f"{key}: {fav_vars.expand(value)}")

        if not lines:
            QtWidgets.QMessageBox.information(self, "提示", "该账号没有可复制的信息")
            return

        text = "\n".join(lines)
        clipboard_recorder.write_to_clipboard(text=text)
        QtWidgets.QMessageBox.information(
            self, "已复制", f"账号信息已复制到剪贴板：\n\n{text}"
        )

    def _copy_value(self, value: str, label: str) -> None:
        """复制单个字段值到剪贴板"""
        if not value:
            return
        clipboard_recorder.write_to_clipboard(text=value)

    def _shorten_for_menu(self, value: str, maxlen: int = 24) -> str:
        """菜单名中展示字段值；过长时截断避免菜单过宽（仅影响显示，复制内容不受影响）"""
        text = str(value)
        return text if len(text) <= maxlen else text[: maxlen] + "…"

    def _show_context_menu(self, pos: QtCore.QPoint) -> None:
        """显示右键菜单"""
        current_item = self.list_widget.itemAt(pos)
        account = None
        if current_item is not None:
            row = self.list_widget.row(current_item)
            self.list_widget.setCurrentRow(row)
            # 用渲染时记录的真实对象，避免 item.data(UserRole) 深拷贝导致
            # 云同步切换写不回 self._accounts，也避免未登录时行号错位。
            account = self._row_to_account.get(row)

        menu = QtWidgets.QMenu(self)

        if account:
            field_labels = [
                ("name", "名称"),
                ("username", "用户名"),
                ("password", "密码"),
                ("server", "服务器"),
                ("notes", "备注"),
            ]
            has_copy_field = False
            for key, label in field_labels:
                value = account.get(key, "")
                if not value:
                    continue
                has_copy_field = True
                shown = value if key == "password" else fav_vars.expand(value)
                action = menu.addAction(f" 复制{label}: {self._shorten_for_menu(shown)}")
                action.triggered.connect(
                    lambda checked=False, v=shown, lb=label: self._copy_value(v, lb)
                )

            # 自定义字段
            custom_fields = account.get("custom_fields", {})
            if custom_fields:
                menu.addSeparator()
                custom_label = QtGui.QAction("--- 自定义字段 ---", menu)
                custom_label.setEnabled(False)
                menu.addAction(custom_label)
                for key, value in custom_fields.items():
                    if not value:
                        continue
                    has_copy_field = True
                    shown = fav_vars.expand(value)
                    action = menu.addAction(f" 复制{key}: {self._shorten_for_menu(shown)}")
                    action.triggered.connect(
                        lambda checked=False, v=shown, lb=key: self._copy_value(v, lb)
                    )

            if has_copy_field or custom_fields:
                menu.addSeparator()

            menu.addAction(" 复制全部信息").triggered.connect(self._copy_account_info)
            menu.addAction(" 复制条目").triggered.connect(
                lambda checked=False, a=account: self._copy_item(a)
            )
            menu.addAction(" 编辑").triggered.connect(self._edit_account)
            menu.addAction(" 删除").triggered.connect(self._remove_account)
            menu.addSeparator()

            # 云标记切换：本地 ↔ 云同步（右键）
            cloud_action = QtGui.QAction(
                "☁ 转为本地" if account.get("cloud") else "☁ 同步到云", self
            )
            cloud_action.triggered.connect(
                lambda checked=False, a=account: self._toggle_cloud(a)
            )
            menu.addAction(cloud_action)

        paste_action = menu.addAction(" 粘贴条目")
        paste_action.setEnabled(_favorites_read_from_clipboard() is not None)
        paste_action.triggered.connect(self._paste_item)

        # 云同步：一键把所有本地项上传到数据库（仅登录后可用）
        if self._cloud_ready():
            menu.addSeparator()
            sync_all_action = QtGui.QAction("☁ 全部同步到数据库", self)
            sync_all_action.triggered.connect(self._sync_all_local_to_cloud)
            menu.addAction(sync_all_action)

        menu.exec(self.list_widget.mapToGlobal(pos))

    # ── 跨标签复制/粘贴 ──
    def _copy_item(self, account: dict) -> None:
        if account:
            _favorites_copy_to_clipboard(account)

    def _paste_item(self) -> None:
        data = _favorites_read_from_clipboard()
        if data is None:
            return
        data = dict(data)
        data.pop("cloud", None)
        data.pop("id", None)
        self._accounts.append(data)
        self._save_favorites()
        self._refresh_list()
