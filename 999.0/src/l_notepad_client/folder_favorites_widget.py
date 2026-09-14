# -*- coding: utf-8 -*-
"""
文件夹收藏标签页 - 嵌入到 l_notepad 的桌面组件
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import ctypes
import ctypes.wintypes
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict

from PySide6 import QtCore, QtGui, QtWidgets, Shiboken

from . import paths
from . import fav_vars
from pytracemp import lprint


# 三个收藏标签页（文件夹/网址/账号）通用的剪贴板条目载荷标记，用于跨标签复制粘贴
FAV_ITEM_CLIPBOARD_MARKER = "__lnp_fav_item__"

#: 收藏列表项图标边长（紧凑：文件夹/命令）
_FAV_ICON_SIZE_COMPACT = 14
#: 网址收藏页图标边长（favicon 本体可见边长，画布贴合无留白）
_FAV_ICON_SIZE_URL = 20
#: 收藏列表项行高与图标边长的差值（即项与项之间的间距）
_FAV_ITEM_GAP_PX = 1
#: 紧凑收藏列表项行高（文件夹/命令页）
_FAV_ITEM_HEIGHT_COMPACT = 22
#: 贴合画布时云角标相对紧凑设计的缩放（1.0=角标/图标比例与原紧凑观感一致）
_FAV_BADGE_SCALE = 0.8


def _favorites_copy_to_clipboard(data: dict) -> None:
    """把一条收藏数据（保留原始字段/类型）序列化到系统剪贴板。"""
    try:
        payload = json.dumps(
            {FAV_ITEM_CLIPBOARD_MARKER: 1, "data": dict(data)},
            ensure_ascii=False,
        )
        QtWidgets.QApplication.clipboard().setText(payload)
    except Exception as e:
        lprint(f"复制条目失败: {e}")


def _favorites_read_from_clipboard() -> dict | None:
    """从系统剪贴板读取收藏条目载荷，非本应用载荷返回 None。"""
    try:
        obj = json.loads(QtWidgets.QApplication.clipboard().text())
    except Exception:
        return None
    if (
        isinstance(obj, dict)
        and obj.get(FAV_ITEM_CLIPBOARD_MARKER)
        and isinstance(obj.get("data"), dict)
    ):
        return obj["data"]
    return None


class FolderFavorite(TypedDict):
    """文件夹收藏条目"""
    name: str
    path: str


class CommandFavorite(TypedDict):
    """命令收藏条目"""
    name: str
    command: str


class UrlFavorite(TypedDict):
    """网址收藏条目"""
    name: str
    url: str


# 兼容旧代码：FavoriteItem 作为联合类型保留
FavoriteItem = dict


# ─────────────────────────────────────────────────────────────────────────
# 收藏项行为父类
#
# 收藏项在磁盘/云端始终以 dict 形式存储（字段：type/name/path|url|command/
# cloud/id...）。历史代码里 “type → 主值字段名” 的映射（folder→path,
# url→url, command→command）以及 “如何打开” 的逻辑散落在右键菜单、
# _execute_item、_rename_item、_fav_cloud_payload 等多处，靠 if item_type==...
# 分支重复判断，极易出现某一处漏改导致操作错条目/取错字段。
#
# 这里把“取主值字段、打开、重命名用的类型”收敛成多态：self.favorites 仍存
# dict（磁盘格式、云同步 API 完全不变），只在需要行为时用
# FavoriteEntry.from_dict(fav) 临时包一层，用完即弃。新增类型只需加一个子类。
# ─────────────────────────────────────────────────────────────────────────
class FavoriteEntry:
    """收藏项行为基类：包裹一个 dict，提供与类型无关的统一操作接口。"""

    #: 该类型主值在 dict 中的键名（子类覆盖）；folder→path / url→url / command→command
    value_key: str = "path"
    #: 该类型标识（子类覆盖），与 dict 里的 "type" 字段一致
    item_type: str = "folder"

    def __init__(self, data: dict) -> None:
        self._data = data

    @staticmethod
    def from_dict(data: dict) -> "FavoriteEntry":
        """按 dict 的 type 字段分发到对应子类；缺省当作文件夹（兼容旧数据）。"""
        item_type = (data or {}).get("type", "folder")
        cls = _ENTRY_TYPES.get(item_type, FolderFavoriteEntry)
        return cls(data)

    # ── 只读属性 ──
    @property
    def data(self) -> dict:
        return self._data

    @property
    def name(self) -> str:
        return self._data.get("name", "")

    @property
    def value(self) -> str:
        """主值（路径 / 网址 / 命令）。优先读本类型字段；旧数据可能缺 type
        或字段不齐，按 path→command→url 兜底，与历史 _fav_cloud_payload 行为一致。"""
        v = self._data.get(self.value_key)
        if v:
            return v
        return (
            self._data.get("path")
            or self._data.get("command")
            or self._data.get("url")
            or ""
        )

    @value.setter
    def value(self, new_value: str) -> None:
        self._data[self.value_key] = new_value

    # ── 内置变量展开（{y}{m}{d} / {pc} / {user} ...，见 fav_vars） ──
    # name/value 保持磁盘与云同步的原始模板；只在执行、显示、复制时展开。
    @property
    def expanded_name(self) -> str:
        return fav_vars.expand(self.name)

    @property
    def expanded_value(self) -> str:
        return fav_vars.expand(self.value)

    # ── 行为（子类覆盖 open） ──
    def open(self, widget: "FolderFavoritesWidget") -> None:  # pragma: no cover - UI
        raise NotImplementedError

    def cloud_payload(self, fallback_kind: str | None = None) -> dict:
        """云同步载荷：统一从主值字段取 value。
        fallback_kind 用于旧数据缺 type 时的回退（历史上取 widget 的 _favorites_kind）。"""
        kind = self._data.get("type") or fallback_kind or self.item_type
        return {"kind": kind, "name": self.name, "value": self.value}


class FolderFavoriteEntry(FavoriteEntry):
    value_key = "path"
    item_type = "folder"

    def open(self, widget: "FolderFavoritesWidget") -> None:  # pragma: no cover - UI
        path = self.expanded_value
        if os.path.exists(path):
            widget._navigate_to_folder(path)
        else:
            QtWidgets.QMessageBox.warning(widget, "警告", f"路径不存在: {path}")


class UrlFavoriteEntry(FavoriteEntry):
    value_key = "url"
    item_type = "url"

    def open(self, widget: "FolderFavoritesWidget") -> None:  # pragma: no cover - UI
        url = self.expanded_value
        if not url:
            return
        # webbrowser.open() 在 Windows 上走 os.startfile()，把 URL 当 Shell 字符串，
        # 遇到无协议头（www.baidu.com / 192.168.1.100:8080）或畸形字符时会被
        # ShellExecute 以畸形目标唤起浏览器，出现异常/崩溃。这里先用
        # QUrl.fromUserInput 规范化（自动补 http://、识别 host:port），再经
        # QDesktopServices.openUrl 交给系统默认处理，并限定 http/https 避免
        # 打开 file:/javascript: 等危险 scheme。
        qurl = QtCore.QUrl.fromUserInput(url)
        if not qurl.isValid() or qurl.scheme() not in ("http", "https"):
            QtWidgets.QMessageBox.warning(widget, "错误", f"网址无效: {url}")
            return
        if QtGui.QDesktopServices.openUrl(qurl):
            lprint(f"打开网址: {qurl.toString()}")
        else:
            QtWidgets.QMessageBox.warning(widget, "错误", f"打开网址失败: {url}")


class CommandFavoriteEntry(FavoriteEntry):
    value_key = "command"
    item_type = "command"

    def open(self, widget: "FolderFavoritesWidget") -> None:  # pragma: no cover - UI
        command = self.expanded_value
        if command:
            try:
                subprocess.Popen(command, shell=True)
                lprint(f"执行命令: {command}")
            except Exception as e:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(widget, "错误", f"执行命令失败: {e}")


_ENTRY_TYPES: dict[str, type[FavoriteEntry]] = {
    "folder": FolderFavoriteEntry,
    "url": UrlFavoriteEntry,
    "command": CommandFavoriteEntry,
}


def attach_var_preview(layout, pairs, tooltip: str | None = None):
    """给编辑对话框挂一行内置变量实时预览，返回 ``(label, refresh_fn)``。

    *pairs* 为 ``[(标签, QLineEdit)]``；任一输入变化即重算展开结果。
    保存的仍是原始模板文本，只在显示/执行/复制时展开。
    联动同步走 ``blockSignals`` 时不会触发刷新，需手动调 ``refresh_fn()``。
    """
    def build() -> str:
        return fav_vars.preview_text(
            [(text, input_widget.text()) for text, input_widget in pairs]
        )

    label = QtWidgets.QLabel(build())
    label.setWordWrap(True)
    label.setStyleSheet(
        "color: #89DDFF; background: rgba(255,255,255,12);"
        " padding: 4px 6px; border-radius: 3px;"
    )
    label.setToolTip(tooltip or f"{fav_vars.VARIABLE_HELP}\n保存时仍存原始模板，仅显示/执行时展开。")

    def refresh(*_args) -> None:
        label.setText(build())

    for _text, input_widget in pairs:
        input_widget.textChanged.connect(refresh)
    layout.addWidget(label)
    return label, refresh


class RenameItemDialog(QtWidgets.QDialog):
    """重命名收藏夹项目的对话框（支持名称和值联动）"""
    
    def __init__(
        self,
        parent: QtWidgets.QWidget,
        item_type: str,
        name: str,
        value: str,
    ) -> None:
        super().__init__(parent)
        self._item_type = item_type
        self._setup_ui(item_type, name, value)
    
    def _setup_ui(self, item_type: str, name: str, value: str) -> None:
        """初始化 UI"""
        title_map = {
            "folder": "修改文件夹",
            "command": "修改命令",
            "url": "修改网址"
        }
        label_map = {
            "folder": "文件夹路径:",
            "command": "执行命令:",
            "url": "网址链接:"
        }
        
        self.setWindowTitle(title_map.get(item_type, "修改项目"))
        self.setMinimumWidth(500)
        
        layout = QtWidgets.QVBoxLayout(self)
        
        # 显示名称输入框
        name_layout = QtWidgets.QHBoxLayout()
        name_label = QtWidgets.QLabel("显示名称:")
        name_label.setFixedWidth(80)
        self.name_input = QtWidgets.QLineEdit(name)
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.name_input)
        layout.addLayout(name_layout)
        
        # 真实值输入框（路径、命令或网址）
        value_layout = QtWidgets.QHBoxLayout()
        value_label = QtWidgets.QLabel(label_map.get(item_type, "值:"))
        value_label.setFixedWidth(80)
        self.value_input = QtWidgets.QLineEdit(value)
        value_layout.addWidget(value_label)
        value_layout.addWidget(self.value_input)
        layout.addLayout(value_layout)
        
        # 锁复选框（默认勾选，表示名称和值联动）
        self.lock_checkbox = QtWidgets.QCheckBox(" 名称和值保持一致")
        self.lock_checkbox.setChecked(True)
        layout.addWidget(self.lock_checkbox)

        # 内置变量实时预览（{y}{m}{d}/{pc}/{user} ...）
        _, self._var_refresh = attach_var_preview(
            layout, [("名称", self.name_input), ("值", self.value_input)]
        )

        # 设置联动逻辑
        self._setup_lock_logic()
        
        # 按钮
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
    
    def _setup_lock_logic(self) -> None:
        """设置锁定/解锁时的联动逻辑"""
        # 保存信号处理函数，以便后续断开
        self._sync_name_to_value = None
        self._sync_value_to_name = None
        
        def on_lock_changed(checked: bool):
            if checked:
                # 锁定时：双向同步
                def sync_name_to_value(text):
                    self.value_input.blockSignals(True)
                    self.value_input.setText(text)
                    self.value_input.blockSignals(False)
                    self._update_preview()
                
                def sync_value_to_name(text):
                    self.name_input.blockSignals(True)
                    self.name_input.setText(text)
                    self.name_input.blockSignals(False)
                    self._update_preview()
                
                # 保存引用以便后续使用
                self._sync_name_to_value = sync_name_to_value
                self._sync_value_to_name = sync_value_to_name
                
                self.name_input.textChanged.connect(self._sync_name_to_value)
                self.value_input.textChanged.connect(self._sync_value_to_name)
            else:
                # 解锁时：断开联动
                try:
                    if self._sync_name_to_value:
                        self.name_input.textChanged.disconnect(self._sync_name_to_value)
                    if self._sync_value_to_name:
                        self.value_input.textChanged.disconnect(self._sync_value_to_name)
                except Exception:
                    pass
        
        self.lock_checkbox.stateChanged.connect(
            lambda state: on_lock_changed(state == 2)
        )
        
        # 初始化时如果是勾选状态，立即连接信号
        if self.lock_checkbox.isChecked():
            on_lock_changed(True)
    
    def _update_preview(self, *_args) -> None:
        """刷新变量预览（联动 setText 走了 blockSignals，需手动补一次）。"""
        self._var_refresh()

    def get_result(self) -> tuple[str, str]:
        """获取修改后的名称和值"""
        return self.name_input.text().strip(), self.value_input.text().strip()


# 剪贴板历史最多保存条数（超出丢弃最旧的）
CLIPBOARD_MAX_STORED = 2000

# 剪贴板图片子目录（PNG 原图落盘，历史条目只存引用，避免 JSON 膨胀）
CLIPBOARD_IMAGES_DIR_NAME = "clipboard_images"

# 图片保存前的最长边上限（超过则等比缩放，防止超大截图撑爆磁盘）
CLIPBOARD_IMAGE_MAX_SIDE = 4096


class ClipboardHistoryModel(QtCore.QAbstractListModel):
    """剪贴板历史数据模型（配合 QListView 虚拟化渲染）。

    QListView 只渲染可见行，因此无论历史多大都不会卡顿，
    去掉了原先的「分批加载更多」逻辑。去重用 set 维护，查找 O(1)。

    支持三种条目（kind）：
    - ``text``：纯文本 ``{kind, text, time}``
    - ``image``：图片 ``{kind, text, time, md5, image_path, width, height}``，
      PNG 原图落盘到 ``favorites_dir/clipboard_images/{md5}.png``，历史只存引用
    - ``file``：复制文件/文件夹 ``{kind, text, time, files, count}``
    """

    TextRole = QtCore.Qt.UserRole            # 主文本（text / 文件展示名 / "[图片]"）
    KindRole = QtCore.Qt.UserRole + 1        # kind: text / image / file
    ImagePathRole = QtCore.Qt.UserRole + 2   # image: PNG 绝对路径
    FilesRole = QtCore.Qt.UserRole + 3       # file: 文件路径列表
    TimeRole = QtCore.Qt.UserRole + 4        # 记录时间
    SearchRole = QtCore.Qt.UserRole + 5      # 搜索过滤用的文本
    ItemRole = QtCore.Qt.UserRole + 6        # 整条 dict（右键/双击用）

    def __init__(self, items: list[dict] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._items: list[dict] = list(items) if items else []
        self._key_set: set[tuple] = {self._dedupe_key(it) for it in self._items}

    # ---- 去重键：按 kind 区分 ----
    @staticmethod
    def _dedupe_key(item: dict) -> tuple:
        kind = item.get("kind", "text") if isinstance(item, dict) else "text"
        if kind == "image":
            return ("image", str(item.get("md5", "") or ""))
        if kind == "file":
            files = sorted(str(f) for f in (item.get("files", []) or []))
            return ("file", "|".join(files))
        return ("text", str(item.get("text", "") or ""))

    # ---- Qt model 接口 ----
    def items(self) -> list[dict]:
        """条目列表（浅拷贝，供纯 Python 快速统计，避免逐行走 Qt 接口）。"""
        return list(self._items)

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index: QtCore.QModelIndex, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None
        item = self._items[index.row()]
        kind = item.get("kind", "text")
        text = str(item.get("text", "") or "")
        time_str = str(item.get("time", "") or "")
        if role == QtCore.Qt.DisplayRole:
            if kind == "image":
                w = int(item.get("width", 0) or 0)
                h = int(item.get("height", 0) or 0)
                t = text if (text and text != "[图片]") else ""
                if len(t) > 24:
                    t = t[:24] + "…"
                if t:
                    return f"[{time_str}] [图片] {w}×{h} {t}"
                return f"[{time_str}] [图片] {w}×{h}"
            if kind == "file":
                files = item.get("files", []) or []
                if len(files) == 1:
                    return f"[{time_str}] 📄 {os.path.basename(files[0])}"
                return f"[{time_str}] 📁 {len(files)} 个文件"
            return f"[{time_str}] {text}"
        if role == QtCore.Qt.ToolTipRole:
            if kind == "image":
                p = item.get("image_path", "")
                if text and text != "[图片]":
                    return f"[图片] {time_str}\n{p}\n{text}"
                return f"[图片] {time_str}\n{p}"
            if kind == "file":
                return "\n".join(item.get("files", []) or [])
            return text
        if role == self.TextRole:
            return text
        if role == self.KindRole:
            return kind
        if role == self.ImagePathRole:
            return item.get("image_path", "")
        if role == self.FilesRole:
            return list(item.get("files", []) or [])
        if role == self.TimeRole:
            return time_str
        if role == self.SearchRole:
            if kind == "image":
                if text and text != "[图片]":
                    return "[图片] " + text
                return "[图片]"
            if kind == "file":
                parts = [os.path.basename(f) for f in (item.get("files", []) or [])]
                parts.extend(item.get("files", []) or [])
                return " ".join(parts)
            return text
        if role == self.ItemRole:
            return item
        return None

    # ---- 业务接口 ----
    def items(self) -> list[dict]:
        return self._items

    def contains_key(self, key: tuple) -> bool:
        return key in self._key_set

    def prepend(self, item: dict, max_stored: int = CLIPBOARD_MAX_STORED) -> None:
        """插入到最前；已存在同 key 则忽略；超出上限时裁剪最旧的若干条。"""
        key = self._dedupe_key(item)
        if key in self._key_set:
            return
        self.beginInsertRows(QtCore.QModelIndex(), 0, 0)
        self._items.insert(0, item)
        self._key_set.add(key)
        self.endInsertRows()
        if len(self._items) > max_stored:
            start, end = max_stored, len(self._items) - 1
            self.beginRemoveRows(QtCore.QModelIndex(), start, end)
            for dropped in self._items[max_stored:]:
                self._key_set.discard(self._dedupe_key(dropped))
            del self._items[max_stored:]
            self.endRemoveRows()

    def remove_key(self, key: tuple) -> dict | None:
        """删除指定 key 的条目，返回被删除的条目（无则 None）。"""
        for i, it in enumerate(self._items):
            if self._dedupe_key(it) == key:
                self.beginRemoveRows(QtCore.QModelIndex(), i, i)
                removed = self._items.pop(i)
                self._key_set.discard(key)
                self.endRemoveRows()
                return removed
        return None

    def reset_items(self, items: list[dict]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._key_set = {self._dedupe_key(it) for it in self._items}
        self.endResetModel()

    def clear(self) -> None:
        self.reset_items([])

    def dedupe(self) -> int:
        """同 key 只保留最新一条（保持时间倒序）。返回移除条数。"""
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for it in self._items:
            key = self._dedupe_key(it)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(it)
        removed = len(self._items) - len(deduped)
        if removed > 0:
            self.reset_items(deduped)
        return removed

    def update_item_at(self, row: int, item: dict) -> bool:
        """更新指定行内容。新 key 与其它行冲突时删除当前行（去重）。

        返回 True 表示已更新；False 表示因冲突删除了该行。
        """
        if not (0 <= row < len(self._items)):
            return False
        old_key = self._dedupe_key(self._items[row])
        new_key = self._dedupe_key(item)
        if new_key != old_key and new_key in self._key_set:
            # 新内容已存在于其它行 → 删除当前行去重
            self.remove_key(old_key)
            return False
        if new_key != old_key:
            self._key_set.discard(old_key)
            self._key_set.add(new_key)
        self._items[row] = item
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, 0),
            [QtCore.Qt.DisplayRole, self.TextRole, self.SearchRole,
             QtCore.Qt.ToolTipRole],
        )
        return True


# 文件行图标提供者（共享实例，避免每行重复创建）
_FILE_ICON_PROVIDER = QtWidgets.QFileIconProvider()


class ClipboardItemDelegate(QtWidgets.QStyledItemDelegate):
    """剪贴板历史渲染 delegate：按 kind 渲染。

    - 文本行：紧凑 16px，显示 ``[time] text``
    - 图片行：左侧缩略图 + 时间/尺寸信息，行高约 56px
    - 文件行：文件图标 + 名称，行高约 26px
    """

    _THUMB_MAX = 56        # 图片行缩略图区域高度（含内边距）
    _FILE_ROW_H = 26       # 文件行高
    _TEXT_ROW_H = 16       # 文本行高
    _THUMB_CACHE_LIMIT = 256

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._thumb_cache: dict = {}
        self._thumb_order: list = []

    @staticmethod
    def _thumb_path_for(path: str):
        """返回同目录下的缩略图文件路径：``{md5}.png`` -> ``{md5}_t.png``。"""
        root, _ext = os.path.splitext(path)
        return f"{root}_t.png"

    def _get_thumb(self, path: str):
        """读取并缓存缩略图（带简单 LRU 上限）。

        优先加载保存时预生成的小图 ``{md5}_t.png``（几 KB，解码极快）；
        若旧历史没有小图，则读原图生成并顺手回填落盘，下次直接读小图，
        避免反复解码 4096px 大图导致呼出卡顿。
        """
        if not path or not os.path.exists(path):
            return None
        thumb_path = self._thumb_path_for(path)
        load_path = thumb_path if os.path.exists(thumb_path) else path
        try:
            mtime = os.path.getmtime(load_path)
        except OSError:
            mtime = 0
        key = (path, mtime)
        cached = self._thumb_cache.get(key)
        if cached is not None:
            return cached
        pm = QtGui.QPixmap(load_path)
        if pm.isNull():
            return None
        pm = pm.scaled(
            self._THUMB_MAX - 10, self._THUMB_MAX - 10,
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        # 旧图首次由原图生成时，顺手把 46px 小图写盘，下次直接读小图
        if load_path == path and not os.path.exists(thumb_path):
            try:
                pm.save(thumb_path, "PNG")
            except OSError:
                pass
        if len(self._thumb_cache) >= self._THUMB_CACHE_LIMIT and self._thumb_order:
            oldest = self._thumb_order.pop(0)
            self._thumb_cache.pop(oldest, None)
        self._thumb_cache[key] = pm
        self._thumb_order.append(key)
        return pm

    def sizeHint(self, option, index):
        # 行高由 kind 决定为固定值：直接返回，跳过基类的字体度量计算，
        # 大列表（2000+ 行）内部布局逐行调用时可显著提速
        kind = index.data(ClipboardHistoryModel.KindRole) or "text"
        if kind == "image":
            h = self._THUMB_MAX
        elif kind == "file":
            h = self._FILE_ROW_H
        else:
            h = self._TEXT_ROW_H
        w = option.rect.width()
        return QtCore.QSize(w if w > 0 else 300, h)

    def paint(self, painter, option, index):
        kind = index.data(ClipboardHistoryModel.KindRole) or "text"
        painter.save()
        try:
            if kind == "image":
                self._paint_image_row(painter, option, index)
            elif kind == "file":
                self._paint_file_row(painter, option, index)
            else:
                self._paint_text_row(painter, option, index)
        finally:
            painter.restore()

    def _draw_background(self, painter, option):
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QtGui.QColor("#3a3a3a"))
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, QtGui.QColor("#333333"))
        else:
            painter.fillRect(option.rect, QtGui.QColor("#1e1e1e"))

    def _draw_elided_text(self, painter, option, text: str, x: int, color: str = "#d4d4d4"):
        font = option.font
        fm = QtGui.QFontMetrics(font)
        elided = fm.elidedText(
            text, QtCore.Qt.ElideRight, max(10, option.rect.right() - x - 6))
        baseline = option.rect.top() + (
            option.rect.height() - fm.height()) // 2 + fm.ascent()
        painter.setPen(QtGui.QColor(color))
        painter.setFont(font)
        painter.drawText(x, baseline, elided)

    def _paint_text_row(self, painter, option, index):
        self._draw_background(painter, option)
        text = index.data(QtCore.Qt.DisplayRole) or ""
        self._draw_elided_text(painter, option, text, option.rect.left() + 6)

    def _paint_image_row(self, painter, option, index):
        self._draw_background(painter, option)
        path = index.data(ClipboardHistoryModel.ImagePathRole) or ""
        thumb = self._get_thumb(path)
        margin = 4
        if thumb is not None:
            tw, th = thumb.width(), thumb.height()
            tx = option.rect.left() + margin
            ty = option.rect.top() + (option.rect.height() - th) // 2
            painter.drawPixmap(QtCore.QRect(tx, ty, tw, th), thumb)
        text = index.data(QtCore.Qt.DisplayRole) or ""
        self._draw_elided_text(painter, option, text,
                               option.rect.left() + self._THUMB_MAX)

    def _paint_file_row(self, painter, option, index):
        self._draw_background(painter, option)
        icon = _FILE_ICON_PROVIDER.icon(
            QtWidgets.QFileIconProvider.IconType.File)
        icon_rect = QtCore.QRect(
            option.rect.left() + 4,
            option.rect.top() + (option.rect.height() - 14) // 2,
            14, 14,
        )
        icon.paint(painter, icon_rect)
        text = index.data(QtCore.Qt.DisplayRole) or ""
        self._draw_elided_text(painter, option, text, option.rect.left() + 22)


class ImageHoverPreview(QtCore.QObject):
    """悬停预览：鼠标悬停在剪贴板历史的图片/文本项上时，在弹窗旁显示预览。

    - 图片项：大图（长边 ≤ 480px）+ 尺寸信息 + 图文并存时的文字内容
    - 纯文本项：完整文字（自动换行，超长截断）
    - 监听 viewport 的 MouseMove/Leave，离开对应行/列表即隐藏
    - 图片/文本行同时吞掉系统 ToolTip，避免两个浮层重叠
    """

    _PREVIEW_MAX = 480      # 预览图长边上限
    _OFFSET_X = 18          # 相对光标的偏移
    _OFFSET_Y = 18
    _TEXT_LIMIT = 200       # 图片预览内附带文字展示上限
    _TEXT_ITEM_LIMIT = 800  # 纯文本条目预览的字符上限

    def __init__(self, list_view: QtWidgets.QListView, parent=None,
                 on_preview=None, anchor_widget=None) -> None:
        super().__init__(parent)
        self._view = list_view
        # 宿主接管预览时（如弹窗左侧面板）不再弹光标浮窗，只把悬停项回调出去
        self._on_preview = on_preview
        # 预览浮窗锚定到某窗口左侧（如剪贴板小窗）；None 则跟随鼠标右侧
        self._anchor = anchor_widget
        self._preview: Optional[QtWidgets.QFrame] = None
        self._img_label: Optional[QtWidgets.QLabel] = None
        self._info_label: Optional[QtWidgets.QLabel] = None
        self._current_key = ""
        vp = self._view.viewport()
        vp.setMouseTracking(True)
        vp.installEventFilter(self)
        self._view.destroyed.connect(self._hide)

    def eventFilter(self, obj, event) -> bool:
        t = event.type()
        if t == QtCore.QEvent.Type.MouseMove:
            index = self._view.indexAt(event.pos())
            item = (
                index.data(ClipboardHistoryModel.ItemRole)
                if index.isValid() else None
            )
            self._update(event.globalPosition().toPoint(), item)
        elif t == QtCore.QEvent.Type.Leave:
            self._hide()
        elif t == QtCore.QEvent.Type.ToolTip:
            # 图片/文本行用自定义预览，吞掉系统 tooltip 避免重叠
            index = self._view.indexAt(event.pos())
            item = (
                index.data(ClipboardHistoryModel.ItemRole)
                if index.isValid() else None
            )
            if item is not None and item.get("kind") in ("image", "text"):
                return True
        return super().eventFilter(obj, event)

    def _update(self, gpos: QtCore.QPoint, item) -> None:
        if self._on_preview is not None:
            # 宿主接管：把当前悬停项交给宿主（如弹窗左侧面板）
            self._on_preview(item)
            return
        if item is None:
            self._hide()
            return
        kind = item.get("kind")
        if kind == "image":
            path = str(item.get("image_path", "") or "")
            if not path or not os.path.exists(path):
                self._hide()
                return
            key = ("image", path, str(item.get("time", "") or ""))
            if key != self._current_key:
                self._current_key = key
                self._load_image(item, path)
        elif kind == "text":
            text = str(item.get("text", "") or "")
            if not text.strip():
                self._hide()
                return
            key = ("text", text[:64], str(item.get("time", "") or ""))
            if key != self._current_key:
                self._current_key = key
                self._load_text(item)
        else:
            self._hide()
            return
        if self._preview is not None:
            self._place(self._preview, gpos)
            self._preview.show()
            self._preview.raise_()

    def _load_image(self, item, path: str) -> None:
        self._ensure_preview()
        self._img_label.show()  # 上次预览可能是纯文本（图片标签被隐藏）
        # QImageReader.setScaledSize 会拉伸到指定尺寸（实测不保持宽高比），
        # 必须先按原图比例算出目标尺寸，再解码缩放（避免加载全像素卡顿）。
        reader = QtGui.QImageReader(path)
        reader.setAutoTransform(True)
        orig = reader.size()
        target = QtCore.QSize(self._PREVIEW_MAX, self._PREVIEW_MAX)
        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            target = orig.scaled(
                self._PREVIEW_MAX, self._PREVIEW_MAX,
                QtCore.Qt.KeepAspectRatio)
            reader.setScaledSize(target)
        qimg = reader.read()
        if qimg.isNull():
            self._hide()
            return
        pm = QtGui.QPixmap.fromImage(qimg)
        # 兜底：个别格式 setScaledSize 仍可能变形 → 强制按比例纠正一次
        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            if pm.width() * orig.height() != pm.height() * orig.width():
                pm = pm.scaled(
                    self._PREVIEW_MAX, self._PREVIEW_MAX,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation)
        self._img_label.setPixmap(pm)
        w = int(item.get("width", 0) or 0) or pm.width()
        h = int(item.get("height", 0) or 0) or pm.height()
        text = str(item.get("text", "") or "")
        if text and text != "[图片]":
            disp = text.replace("\r", " ").replace("\n", " ")
            if len(disp) > self._TEXT_LIMIT:
                disp = disp[: self._TEXT_LIMIT] + "…"
            self._info_label.setText(f"{w}×{h}\n\n{disp}")
        else:
            self._info_label.setText(f"{w}×{h}")
        self._preview.adjustSize()

    def _load_text(self, item) -> None:
        """纯文本条目悬停预览：显示完整文字（自动换行，超长截断）。"""
        self._ensure_preview()
        self._img_label.hide()
        text = str(item.get("text", "") or "")
        if len(text) > self._TEXT_ITEM_LIMIT:
            text = text[: self._TEXT_ITEM_LIMIT] + "…"
        self._info_label.setText(text)
        self._preview.adjustSize()

    def _ensure_preview(self) -> None:
        # 无父顶级浮窗可能被 C++ 侧提前销毁，失效则重建
        if self._preview is not None:
            try:
                if Shiboken.isValid(self._preview):
                    return
            except Exception:
                pass
            self._preview = None
            self._img_label = None
            self._info_label = None
        flags = (
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self._preview = QtWidgets.QFrame(None, flags)
        self._preview.setObjectName("ClipboardImagePreview")
        self._preview.setMaximumHeight(self._PREVIEW_MAX + 80)
        self._preview.setStyleSheet(
            "QFrame#ClipboardImagePreview { background-color: #2b2b2b;"
            " border: 1px solid #5a5a5a; border-radius: 6px; }"
            "QLabel { color: #d4d4d4; background: transparent; }"
        )
        lay = QtWidgets.QVBoxLayout(self._preview)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        self._img_label = QtWidgets.QLabel()
        self._img_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet(
            "background: #1e1e1e; border: 1px solid #3c3c3c;"
        )
        lay.addWidget(self._img_label)
        self._info_label = QtWidgets.QLabel()
        self._info_label.setWordWrap(True)
        self._info_label.setMaximumWidth(self._PREVIEW_MAX + 16)
        lay.addWidget(self._info_label)

    def _place(self, w: QtWidgets.QWidget, gpos: QtCore.QPoint) -> None:
        anchor = self._anchor
        if anchor is not None:
            # 根据弹窗到屏幕边缘的距离，智能选择显示在弹窗的 顶部/左侧/底部/右侧
            try:
                self._place_around_anchor(w, anchor, gpos)
                return
            except Exception:
                pass
        x = gpos.x() + self._OFFSET_X
        y = gpos.y() + self._OFFSET_Y
        scr = QtWidgets.QApplication.screenAt(gpos)
        if scr is not None:
            geo = scr.availableGeometry()
            x = max(geo.left(), min(x, geo.right() - w.width()))
            y = max(geo.top(), min(y, geo.bottom() - w.height()))
        w.move(x, y)

    def _place_around_anchor(self, w: QtWidgets.QWidget,
                             anchor: QtWidgets.QWidget,
                             gpos: QtCore.QPoint) -> None:
        """按弹窗四周可用空间智能放置预览浮窗。

        优先级：顶部 → 左侧 → 底部 → 右侧（取第一个空间放得下的方向）；
        都放不下时选空间最大的方向硬放，最后统一 clamp 到屏幕内。
        """
        fr = anchor.frameGeometry()
        scr = QtWidgets.QApplication.screenAt(
            QtCore.QPoint(fr.center().x(), fr.center().y()))
        if scr is None:
            scr = QtWidgets.QApplication.screenAt(gpos)
        geo = scr.availableGeometry() if scr is not None else None
        w.adjustSize()
        pw = w.width()
        ph = w.height()
        if geo is None:
            w.move(fr.left() - pw - 4, gpos.y() - ph // 2)
            return
        top_space = fr.top() - geo.top()
        left_space = fr.left() - geo.left()
        bottom_space = geo.bottom() - fr.bottom()
        right_space = geo.right() - fr.right()
        # 左侧/右侧水平放置：垂直跟随鼠标；顶部/底部垂直放置：水平居中于弹窗
        yc = gpos.y() - ph // 2
        xc = fr.center().x() - pw // 2
        candidates = [
            (top_space, ph, (xc, fr.top() - ph - 4)),       # 顶部
            (left_space, pw, (fr.left() - pw - 4, yc)),     # 左侧
            (bottom_space, ph, (xc, fr.bottom() + 4)),      # 底部
            (right_space, pw, (fr.right() + 4, yc)),        # 右侧
        ]
        pos = None
        for space, need, p in candidates:
            if space >= need:
                pos = p
                break
        if pos is None:
            pos = max(candidates, key=lambda c: c[0])[2]
        x = max(geo.left(), min(pos[0], geo.right() - pw))
        y = max(geo.top(), min(pos[1], geo.bottom() - ph))
        w.move(x, y)

    def _hide(self) -> None:
        self._current_key = ""
        if self._preview is not None:
            try:
                self._preview.hide()
            except Exception:
                pass


class ClipboardHistoryPopup(QtWidgets.QFrame):
    """Win+V 弹出的「仅剪贴板历史」小窗口。

    - 无边框、置顶、弹出时不抢调用者焦点（避免调用程序失焦退出编辑态），
      点击弹窗/在其它处点击或切走窗口时自动隐藏（仿系统剪贴板历史）
    - 与源面板共享 ClipboardHistoryModel；双击/回车把条目回填到调用窗口并粘贴
    """

    # 无边框窗口的拖动手柄高度（顶部标题行区域）
    _drag_zone_height = 30
    # 弹窗与光标的间距：既要避开鼠标箭头延伸区（约 12~19px），也留出明显距离
    _cursor_gap_placeholder = None
    _cursor_gap = 28

    def __init__(self, source: "FolderFavoritesWidget", parent=None) -> None:
        flags = (
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(parent, flags)
        # 弹出时不抢调用者窗口的焦点：否则调用程序的编辑态（如 item 编辑模式）
        # 会被失焦事件推回显示/预览模式
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # 弹窗不激活，需自行探测「在别处点击/切走窗口」以实现自动隐藏
        self._watch_timer = QtCore.QTimer(self)
        self._watch_timer.setInterval(60)
        self._watch_timer.timeout.connect(self._poll_outside_watch)
        self._watch_btn_down = False
        self._watch_fg_hwnd = 0
        self._last_use_ts = 0.0  # _use_item 去重时间戳（双击会同时触发 doubleClicked+activated）
        self._source = source
        self._caller_hwnd: int = 0
        self._count_label: Optional[QtWidgets.QLabel] = None
        self._search: Optional[QtWidgets.QLineEdit] = None
        self._proxy: Optional[QtCore.QSortFilterProxyModel] = None
        self._list: Optional[QtWidgets.QListView] = None
        # 无边框拖动状态
        self._dragging: bool = False
        self._drag_offset: QtCore.QPoint = QtCore.QPoint()
        self.setObjectName("ClipboardHistoryPopup")
        self.setStyleSheet(
            """
            QFrame#ClipboardHistoryPopup {
                background-color: #252526;
                border: 1px solid #3c3c3c;
                border-radius: 8px;
            }
            QLabel { color: #d4d4d4; background: transparent; }
            QLineEdit#popup_search {
                background-color: #1e1e1e; border: 1px solid #3c3c3c; color: #d4d4d4;
                border-radius: 4px; padding: 4px 6px;
            }
            QListView#popup_list {
                background-color: #1e1e1e; border: 1px solid #3c3c3c; color: #d4d4d4;
                border-radius: 4px; padding: 2px;
            }
            QListView#popup_list::item { min-height: 16px; }
            QListView#popup_list::item:selected { background-color: #3a3a3a; }
            QToolButton { color: #cccccc; border: none; background: transparent; }
            QToolButton:hover { background: rgba(255,255,255,0.1); border-radius: 4px; }
            """
        )
        self._setup_ui()
        # 注意：这里刻意【不加】QGraphicsDropShadowEffect。
        # 整窗实时阴影会让无边框窗口走软件离屏渲染，每次显示/重绘都要对整个窗口
        # （含 2000 条历史列表 + 图片缩略图）逐帧高斯模糊，呼出瞬间 CPU 飙升、
        # 系统明显卡顿。用样式表的 1px 边框代替即可，性能优先。

    def _setup_ui(self) -> None:
        self.setFixedWidth(340)
        self.setMinimumHeight(200)
        self.setMaximumHeight(520)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("📋 剪贴板历史")
        title.setStyleSheet("font-size: 13px; font-weight: bold;")
        self._count_label = QtWidgets.QLabel("0 条")
        self._count_label.setStyleSheet("color: #89DDFF; font-size: 11px;")
        clear_btn = QtWidgets.QToolButton()
        clear_btn.setText("🗑 清除")
        clear_btn.setToolTip("清除全部历史")
        clear_btn.clicked.connect(self._clear_all)
        close_btn = QtWidgets.QToolButton()
        close_btn.setText("✕")
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._close_popup)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self._count_label)
        title_row.addWidget(clear_btn)
        title_row.addWidget(close_btn)
        lay.addLayout(title_row)

        self._search = QtWidgets.QLineEdit()
        self._search.setObjectName("popup_search")
        self._search.setPlaceholderText(" 搜索剪贴板历史...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        lay.addWidget(self._search)

        self._proxy = QtCore.QSortFilterProxyModel(self)
        self._proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self._proxy.setFilterRole(ClipboardHistoryModel.SearchRole)

        self._list = QtWidgets.QListView()
        self._list.setObjectName("popup_list")
        self._list.setModel(self._proxy)
        self._list.setItemDelegate(ClipboardItemDelegate(self))
        # 图片项悬停预览（锚定在弹窗左侧）；双击打开独立 QDialog 大图预览/编辑
        self._hover_preview = ImageHoverPreview(self._list, anchor_widget=self)
        self._list.setUniformItemSizes(False)
        # 非统一行高时首次布局需逐行向 delegate 要行高，2000 条一次性完成会
        # 冻结界面数百 ms~1s+：改用批量布局模式，每批 100 行即让渡事件循环，
        # 预热与真实 Win+V 呼出都不再卡死 UI（Qt 官方对超大列表的标准解法）。
        self._list.setLayoutMode(QtWidgets.QListView.LayoutMode.Batched)
        self._list.setBatchSize(100)
        self._list.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setSpacing(2)
        self._list.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._show_context_menu)
        # 双击 → 粘贴到调用程序；Ctrl+双击 → 独立编辑预览窗口；回车 → 使用
        self._list.doubleClicked.connect(self._on_double_click)
        self._list.activated.connect(self._use_item)
        lay.addWidget(self._list, 1)

    # ── 显示 ──
    def show_popup(self, caller_hwnd: int = 0, at_cursor: bool = True) -> None:
        self._caller_hwnd = int(caller_hwnd or 0)
        # 模型未变时不重复绑定：重复 setSourceModel/invalidate 会重置代理、
        # 令列表的行高缓存失效，每次弹出都重新全量布局 2000 行（慢）。
        # 数据增删改由模型信号自动传导给代理，无需手动 invalidate。
        if self._proxy.sourceModel() is not self._source.clipboard_model:
            self._proxy.setSourceModel(self._source.clipboard_model)
        if self._search is not None:
            self._search.clear()
        self._update_count()
        # 按内容撑高（内部自取真实尺寸），再按光标避让摆放
        self._fit_height_to_content()
        if at_cursor:
            self._move_near_cursor(QtGui.QCursor.pos())
        self.show()
        # 刻意不 raise_/activateWindow()/setFocus：抢焦点会让调用程序失焦
        # （如正在编辑的 item 因 focusOut 退出编辑模式回预览）。
        # 点击弹窗仍会自然激活，之后由原 focusOut 自动隐藏逻辑接管。
        self._watch_fg_hwnd = self._get_foreground_hwnd()
        # 打开那一刻若已有鼠标键按着，必须记成「本来就按着」：Shift+中键 正是这种情形——
        # 弹窗在中键「按下」沿弹出、手指还按着中键，而弹窗又刻意停在光标旁（避开光标 28px），
        # 于是首次轮询会把这次按下当成新的「点到别处」→ 立刻 hide（表现为闪现）。
        self._watch_btn_down = self._any_button_down()
        self._watch_timer.start()

    @staticmethod
    def _any_button_down() -> bool:
        """是否有鼠标键处于按住状态（左/右/中）。

        「点击别处即隐藏」的判断要用它两次：打开弹窗时记录初始状态、轮询时比下降沿。
        必须是同一份实现，否则两处对「按着没按着」的判断会不一致。
        """
        if sys.platform != "win32":
            return False
        try:
            user32 = ctypes.windll.user32
            return any(
                bool(user32.GetAsyncKeyState(vk) & 0x8000)
                for vk in (0x01, 0x02, 0x04)   # 左 / 右 / 中
            )
        except Exception:
            return False

    def _move_near_cursor(self, pos: QtCore.QPoint) -> None:
        """在光标旁摆放弹窗并避免遮挡光标。

        优先光标左下方（保持原有习惯），屏幕放不下时依次尝试
        右下/左上/右上；四向都放不下取溢出最小的方向 clamp 到屏内。
        上下方向统一留 _cursor_gap 间距，避开鼠标箭头延伸区域。
        """
        pw, ph = self.width(), self.height()
        gap = self._cursor_gap
        candidates = [
            (pos.x() - pw + 12, pos.y() + gap),       # 左下（默认）
            (pos.x() + gap, pos.y() + gap),           # 右下
            (pos.x() - pw + 12, pos.y() - ph - gap),  # 左上
            (pos.x() + gap, pos.y() - ph - gap),      # 右上
        ]
        scr = QtWidgets.QApplication.screenAt(pos)
        geo = scr.availableGeometry() if scr is not None else None
        if geo is None:
            self.move(candidates[0][0], candidates[0][1])
            return
        for x, y in candidates:
            if (geo.left() <= x <= geo.right() - pw
                    and geo.top() <= y <= geo.bottom() - ph):
                self.move(x, y)
                return
        # 四向都放不下（极小分辨率/大 DPI 缩放）：取溢出最小方向 clamp 到屏内
        def _overflow(xy) -> int:
            x, y = xy
            return (
                max(0, geo.left() - x) + max(0, x - (geo.right() - pw))
                + max(0, geo.top() - y) + max(0, y - (geo.bottom() - ph))
            )
        bx, by = min(candidates, key=_overflow)
        self.move(
            max(geo.left(), min(bx, geo.right() - pw)),
            max(geo.top(), min(by, geo.bottom() - ph)),
        )

    def _fit_height_to_content(self) -> None:
        """按列表内容把窗口撑高（上限 maxHeight，下限 minHeight）。

        QListView 是滚动区：sizeHint 固定、不随条目数增长，不处理的话
        窗口会一直停在布局 sizeHint（约 276px），setMaximumHeight(520)
        永远没机会生效；条目多时可见行太少，得频繁滚动。
        """
        # 未过滤时用纯 Python 累加 kind 固定行高：避免逐行 sizeHintForRow
        # 的 C++→Python 往返（2000 行可达数百 ms），超过上限即提前终止
        self.adjustSize()  # 先布局一次，确保 chrome/列表高度可测
        if self._list is None or self._proxy is None:
            return
        chrome = max(60, self.height() - self._list.height())
        max_h = self.maximumHeight()
        if self._proxy.filterRegularExpression().pattern():
            n = self._proxy.rowCount()
            if n <= 0:
                return
            total = sum(self._list.sizeHintForRow(r) for r in range(n))
            total += self._list.spacing() * (n + 1)
        else:
            kind_h = {"image": ClipboardItemDelegate._THUMB_MAX,
                      "file": ClipboardItemDelegate._FILE_ROW_H,
                      "text": ClipboardItemDelegate._TEXT_ROW_H}
            spacing = self._list.spacing()
            total = 0
            for it in self._source.clipboard_model.items():
                total += kind_h.get(it.get("kind", "text"),
                                    ClipboardItemDelegate._TEXT_ROW_H) + spacing
                if chrome + total + 4 >= max_h:
                    break  # 已达窗口上限，继续累加无意义
            if total <= 0:
                return
        want = max(self.minimumHeight(), min(max_h, chrome + total + 4))
        if want != self.height():
            self.resize(self.width(), want)

    def _update_count(self) -> None:
        if self._count_label is not None:
            self._count_label.setText(f"{self._proxy.rowCount()} 条")

    def _on_search(self, text: str) -> None:
        self._proxy.setFilterFixedString(text.strip())
        self._update_count()

    # ── 查看/编辑 ──
    def _open_item_editor(self, index: QtCore.QModelIndex) -> None:
        item = index.data(ClipboardHistoryModel.ItemRole)
        if not item:
            return
        src = self._proxy.mapToSource(index)
        row = src.row() if src.isValid() else -1
        dlg = ClipboardItemEditorDialog(self._source, item, row, parent=None)
        dlg.exec()
        self._update_count()

    # ── 使用 ──
    def _on_double_click(self, index: QtCore.QModelIndex) -> None:
        """双击分发：普通双击 → 粘贴到调用程序；Ctrl+双击 → 独立编辑预览窗口。"""
        mods = QtWidgets.QApplication.keyboardModifiers()
        if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
            self._open_item_editor(index)
        else:
            self._use_item(index)

    def _use_item(self, index: QtCore.QModelIndex) -> None:
        # 双击会同时发出 doubleClicked 与 activated（回车只有 activated），
        # 不去重会连发两次 Ctrl+V，条目内容在调用程序输入框里出现两遍
        now = time.time()
        if now - self._last_use_ts < 0.3:
            return
        self._last_use_ts = now
        item = index.data(ClipboardHistoryModel.ItemRole)
        if not item:
            return
        self._source._apply_clipboard_item_to_system(item)
        self.hide()
        self._paste_to_caller()

    def _paste_to_caller(self) -> None:
        hwnd = int(self._caller_hwnd) if self._caller_hwnd else 0
        if not hwnd or sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return
            self.hide()
            QtWidgets.QApplication.processEvents()
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            QtCore.QTimer.singleShot(80, self._send_paste_shortcut)
        except Exception as e:
            lprint(f"粘贴到调用窗口失败: {e}")

    def _send_paste_shortcut(self) -> None:
        try:
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_V = 0x56
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_V, 0, 0, 0)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            lprint(f"发送粘贴快捷键失败: {e}")

    def _clear_all(self) -> None:
        self._source.clipboard_model.clear()
        self._source._clear_clipboard_image_dir()
        self._source._schedule_save_clipboard()
        self._update_count()

    # ── 右键菜单 ──
    def _item_at(self, point: QtCore.QPoint):
        index = self._list.indexAt(point)
        if not index.isValid():
            return None
        return index.data(ClipboardHistoryModel.ItemRole)

    def _show_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._item_at(position)
        if not item:
            return
        menu = QtWidgets.QMenu(self)
        act_view = menu.addAction(" 查看/编辑")
        act_view.triggered.connect(
            lambda: self._open_item_editor(self._list.indexAt(position)))
        act_use = menu.addAction(" 使用此条")
        act_use.triggered.connect(
            lambda: self._use_item(self._list.indexAt(position)))
        act_del = menu.addAction(" 删除此条")
        act_del.triggered.connect(
            lambda: self._source._delete_clipboard_item(item))
        menu.addSeparator()
        act_clear = menu.addAction(" 清除全部历史")
        act_clear.triggered.connect(self._clear_all)
        menu.exec(self._list.viewport().mapToGlobal(position))
        self._update_count()

    # ── 无边框窗口拖动（按住顶部标题行区域移动） ──
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if (
            event.button() == QtCore.Qt.MouseButton.LeftButton
            and event.position().y() <= self._drag_zone_height
        ):
            self._dragging = True
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._dragging and (
            event.buttons() & QtCore.Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _close_popup(self) -> None:
        self.hide()

    # ── 不抢焦点弹出：外界操作监视（点别处/切走 → 自动隐藏） ──
    @staticmethod
    def _get_foreground_hwnd() -> int:
        if sys.platform != "win32":
            return 0
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            return int(user32.GetForegroundWindow() or 0)
        except Exception:
            return 0

    def _poll_outside_watch(self) -> None:
        """弹窗未抢焦点期间：用户在别处点击/切走窗口 → 自动隐藏。"""
        if not self.isVisible():
            self._watch_timer.stop()
            return
        if self.isActiveWindow():
            # 已激活（用户点击了弹窗），改由原 focusOut 自动隐藏逻辑接管
            self._watch_timer.stop()
            return
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            # 前台窗口已变更且新前台不是本进程（如 Alt+Tab 切走） → 隐藏
            fg = self._get_foreground_hwnd()
            if fg and self._watch_fg_hwnd and fg != self._watch_fg_hwnd:
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)]
                user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
                if int(pid.value) != int(
                        ctypes.windll.kernel32.GetCurrentProcessId()):
                    self.hide()
                    return
            # 任一鼠标键按下沿：点击点不在弹窗（含悬停预览层）内 → 隐藏；
            # 点在弹窗内则交给 Windows 自然激活本窗口（键盘操作随之可用）
            btn_down = self._any_button_down()
            if btn_down and not self._watch_btn_down:
                pt = ctypes.wintypes.POINT()
                user32.GetCursorPos.argtypes = [
                    ctypes.POINTER(ctypes.wintypes.POINT)]
                inside = False
                if user32.GetCursorPos(ctypes.byref(pt)):
                    pos = QtCore.QPoint(pt.x, pt.y)
                    inside = self.geometry().contains(pos)
                    prev = getattr(self, "_hover_preview", None)
                    pv = (getattr(prev, "_preview", None)
                          if prev is not None else None)
                    if not inside and pv is not None and pv.isVisible():
                        inside = pv.geometry().contains(pos)
                if not inside:
                    self.hide()
                    return
            self._watch_btn_down = btn_down
        except Exception:
            pass

    # ── 失焦自动隐藏 ──
    def focusOutEvent(self, event: QtGui.QFocusEvent) -> None:
        super().focusOutEvent(event)
        QtCore.QTimer.singleShot(120, self._maybe_hide)

    def _maybe_hide(self) -> None:
        try:
            if QtWidgets.QApplication.activeWindow() is not self:
                self.hide()
        except Exception:
            pass

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._watch_timer.stop()
        # 隐藏弹窗时一并收起悬停预览浮层
        prev = getattr(self, "_hover_preview", None)
        if prev is not None:
            prev._hide()
        super().hideEvent(event)


class _ZoomableImageLabel(QtWidgets.QLabel):
    """可缩放图片标签：按实际宽高比显示，Ctrl+滚轮缩放。"""

    _MIN_ZOOM = 0.05
    _MAX_ZOOM = 12.0
    _FIT_MAX = 640  # 初始适配尺寸（长边上限）

    zoom_changed = QtCore.Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #151515;")
        self._full_image: Optional[QtGui.QImage] = None
        self._zoom = 1.0
        # 左键拖拽平移（在 QScrollArea 内）：按下变抓手，拖动移动滚动条
        self._scroll_area: Optional[QtWidgets.QScrollArea] = None
        self._panning = False
        self._pan_pos = QtCore.QPointF()
        self._pan_hbar = None
        self._pan_vbar = None
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)

    def set_image(self, qimage: QtGui.QImage) -> None:
        self._full_image = qimage
        self._zoom = 1.0
        self._fit()

    def _fit(self) -> None:
        if self._full_image is None:
            return
        pm = QtGui.QPixmap.fromImage(self._full_image)
        pm = pm.scaled(self._FIT_MAX, self._FIT_MAX,
                       QtCore.Qt.KeepAspectRatio,
                       QtCore.Qt.SmoothTransformation)
        self._zoom = pm.width() / max(1, self._full_image.width())
        self._apply()

    def zoom_by(self, factor: float) -> None:
        if self._full_image is None:
            return
        new_zoom = max(self._MIN_ZOOM,
                       min(self._MAX_ZOOM, self._zoom * factor))
        if abs(new_zoom - self._zoom) < 1e-6:
            return
        self._zoom = new_zoom
        self._apply()

    def _apply(self) -> None:
        w = max(1, int(self._full_image.width() * self._zoom))
        h = max(1, int(self._full_image.height() * self._zoom))
        pm = QtGui.QPixmap.fromImage(self._full_image).scaled(
            w, h, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        self.setPixmap(pm)
        self.resize(pm.size())
        self.zoom_changed.emit(f"{self._zoom * 100:.0f}%")

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            self.zoom_by(1.15 if delta > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

    # ── 左键拖拽平移 ──
    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if (event.button() == QtCore.Qt.MouseButton.LeftButton
                and self._scroll_area is not None):
            self._panning = True
            self._pan_pos = event.position()
            self._pan_hbar = self._scroll_area.horizontalScrollBar()
            self._pan_vbar = self._scroll_area.verticalScrollBar()
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._panning:
            delta = event.position() - self._pan_pos
            self._pan_pos = event.position()
            if self._pan_hbar is not None:
                self._pan_hbar.setValue(
                    self._pan_hbar.value() - int(delta.x()))
            if self._pan_vbar is not None:
                self._pan_vbar.setValue(
                    self._pan_vbar.value() - int(delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._panning:
            self._panning = False
            self._pan_pos = QtCore.QPointF()
            self._pan_hbar = None
            self._pan_vbar = None
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ClipboardItemEditorDialog(QtWidgets.QDialog):
    """独立 QDialog 预览/编辑单条剪贴板历史。

    - 图片：按实际宽高比显示大图（QScrollArea 内可滚动），Ctrl+滚轮缩放，
      附带文字可编辑
    - 文本：可编辑
    - 文件：只读路径列表
    - 支持「保存到历史」（写回模型并落盘）和「复制到剪贴板」
    """

    def __init__(self, source: "FolderFavoritesWidget", item: dict,
                 row: int, parent=None) -> None:
        super().__init__(parent)
        self._source = source
        self._item = dict(item)
        self._row = int(row)
        self.setWindowTitle("预览 / 编辑剪贴板历史")
        self.setModal(True)
        self.resize(720, 620)
        self.setStyleSheet(
            "QDialog { background-color: #252526; }"
            "QLabel { color: #d4d4d4; background: transparent; }"
            "QPlainTextEdit { background-color: #1e1e1e; color: #d4d4d4;"
            " border: 1px solid #3c3c3c; border-radius: 4px; }"
            "QPushButton { background-color: #3a3a3a; color: #d4d4d4;"
            " border: 1px solid #5a5a5a; border-radius: 4px; padding: 4px 14px; }"
            "QPushButton:hover { background-color: #4a4a4a; }"
        )
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        kind = self._item.get("kind", "text")
        info = QtWidgets.QLabel(
            {"image": "图片 + 文字", "text": "文本", "file": "文件"}
            .get(kind, kind))
        info.setStyleSheet("color: #89DDFF; font-size: 11px;")
        lay.addWidget(info)

        self._text_edit = QtWidgets.QPlainTextEdit()
        if kind == "image":
            # 按实际比例预览 + Ctrl+滚轮缩放（QScrollArea 内可滚动看大图）
            self._image_label = _ZoomableImageLabel()
            qimg = self._load_qimage(self._item.get("image_path", ""))
            if qimg is not None:
                self._image_label.set_image(qimg)
            else:
                self._image_label.setText("（无法加载图片）")
            scroll = QtWidgets.QScrollArea()
            scroll.setWidget(self._image_label)
            scroll.setWidgetResizable(False)
            scroll.setStyleSheet(
                "QScrollArea { background-color: #151515;"
                " border: 1px solid #3c3c3c; border-radius: 4px; }")
            # 图片:文字笔记 = 4:1（图片区占大比例）
            lay.addWidget(scroll, 4)
            # 左键拖拽平移图片
            self._image_label._scroll_area = scroll
            meta = QtWidgets.QLabel()
            meta.setStyleSheet("color: #9e9e9e; font-size: 11px;")
            meta.setText(
                f"{self._item.get('width', '?')}×{self._item.get('height', '?')}"
                " ｜ Ctrl+滚轮缩放")
            self._image_label.zoom_changed.connect(
                lambda z: meta.setText(
                    f"{self._item.get('width', '?')}"
                    f"×{self._item.get('height', '?')} ｜ {z}"))
            lay.addWidget(meta)
            text_label = QtWidgets.QLabel("附带文字（可编辑）：")
            text_label.setStyleSheet("font-size: 11px; color: #9e9e9e;")
            lay.addWidget(text_label)
            self._text_edit.setPlaceholderText("该图片对应的文字内容")
            self._text_edit.setPlainText(self._item.get("text", "") or "")
            lay.addWidget(self._text_edit, 1)
        elif kind == "file":
            self._text_edit.setReadOnly(True)
            self._text_edit.setPlainText(
                "\n".join(self._item.get("files", []) or []))
            lay.addWidget(self._text_edit, 1)
        else:
            self._text_edit.setPlaceholderText("剪贴板文字内容")
            self._text_edit.setPlainText(self._item.get("text", "") or "")
            lay.addWidget(self._text_edit, 1)

        btns = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("保存到历史")
        save_btn.clicked.connect(self._save)
        copy_btn = QtWidgets.QPushButton("复制到剪贴板")
        copy_btn.clicked.connect(self._copy)
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(save_btn)
        btns.addWidget(copy_btn)
        btns.addWidget(close_btn)
        lay.addLayout(btns)

    def _load_qimage(self, path: str):
        """读取原始 QImage（保持全尺寸，供按比例缩放预览）。"""
        if not path or not os.path.exists(path):
            return None
        reader = QtGui.QImageReader(path)
        reader.setAutoTransform(True)
        qimg = reader.read()
        if qimg.isNull():
            return None
        return qimg

    def _collect_text(self) -> str:
        kind = self._item.get("kind", "text")
        if kind not in ("text", "image"):
            return str(self._item.get("text", "") or "")
        if kind == "text":
            return self._text_edit.toPlainText().strip()
        t = self._text_edit.toPlainText()
        if not t.strip():
            return "[图片]"
        ts = t.strip()
        return ts[:500] + ("…" if len(ts) > 500 else "")

    def _save(self) -> None:
        new_item = dict(self._item)
        new_item["text"] = self._collect_text()
        ok = self._source.clipboard_model.update_item_at(self._row, new_item)
        if ok:
            self._source._schedule_save_clipboard()
        self.accept()

    def _copy(self) -> None:
        new_item = dict(self._item)
        new_item["text"] = self._collect_text()
        self._source._apply_clipboard_item_to_system(new_item)


def _icon_with_cloud(
    base_icon: QtGui.QIcon,
    synced: bool,
    base_px: int = _FAV_ICON_SIZE_COMPACT,
    *,
    tight: bool = False,
) -> QtGui.QIcon:
    """组合图标：类型图标 + 右下角云角标（云同步=亮蓝，本地=灰）。

    base_px 为类型图标边长。
    - tight=False（紧凑页沿用）：画布留出角标外扩空间，图标本体只占画布约 2/3；
    - tight=True（网址收藏页）：画布贴合图标本体、云角标压在右下角内，
      这样 setIconSize(base_px) 后可见图标正好填满图标框，行间距才真的是设定的值。
    """
    color = QtGui.QColor("#5eb8f5") if synced else QtGui.QColor("#7a7a7a")
    if tight:
        canvas = max(1, base_px)
        pm = QtGui.QPixmap(canvas, canvas)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pm)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.drawPixmap(0, 0, base_icon.pixmap(base_px, base_px))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(color)
        # 角标（紧凑设计的角标簇）按同比例缩到画布右下角内部
        k = base_px / float(_FAV_ICON_SIZE_COMPACT) * _FAV_BADGE_SCALE
        origin_x = canvas - 10 * k
        origin_y = canvas - 7 * k
        for x, y, w, h in ((12, 14, 5, 4), (15, 11, 6, 5), (18, 14, 4, 3)):
            painter.drawEllipse(
                round(origin_x + (x - 12) * k),
                round(origin_y + (y - 11) * k),
                max(2, round(w * k)),
                max(2, round(h * k)),
            )
        painter.end()
        return QtGui.QIcon(pm)

    scale = base_px / float(_FAV_ICON_SIZE_COMPACT)
    canvas = max(1, round(22 * scale))
    pm = QtGui.QPixmap(canvas, canvas)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    painter.drawPixmap(0, round(3 * scale), base_icon.pixmap(base_px, base_px))
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(color)
    for x, y, w, h in ((12, 14, 5, 4), (15, 11, 6, 5), (18, 14, 4, 3)):
        painter.drawEllipse(
            round(x * scale), round(y * scale), round(w * scale), round(h * scale)
        )
    painter.end()
    return QtGui.QIcon(pm)


# ─────────────────────────────────────────────────────────────────────────
# 网址收藏 favicon（网页图标）自动获取
#
# 添加/加载网址收藏时，在后台线程自动访问一次该网址取站点图标：优先直接
# GET 站点根 /favicon.ico（一次请求最快最稳，SPA 站点也适用），取不到再抓
# 页面 HTML 解析 <link rel="icon">。图标转 PNG 缓存到
# favorites/url_icons/<md5(scheme://host)>.png。列表渲染时直接读缓存。
#
# 缓存键只取协议+主机，与路径/模板变量（{date:...} 等）无关，因此每日变化
# 的 URL 也不会重复抓取。抓取失败写 <key>.fail 标记（记录时间与原因），按
# 原因分 TTL 不再重试：
#   - network_error（超时/连接失败）：5 分钟后可重试，避免偶发抖动卡一整天
#   - no_icon（确实访问到了但站点没有图标）：1 天后才重试，避免反复请求
# ─────────────────────────────────────────────────────────────────────────

#: 网址图标缓存子目录名（favorites/url_icons）
_URL_ICONS_DIR_NAME = "url_icons"
#: 站点确实没有图标时的失败标记有效期（秒）
_URL_ICON_FAIL_TTL_SEC = 24 * 3600
#: 网络/超时失败的重试间隔（秒），偶发抖动不长期卡住
_URL_ICON_NET_ERR_TTL_SEC = 5 * 60
#: 同时进行的图标抓取线程上限（避免大量收藏一次拉爆连接）
_URL_ICON_MAX_CONCURRENT = 3
#: 单个抓取请求超时（秒，内网站点首连/SSL 握手可能较慢）
_URL_ICON_TIMEOUT_SEC = 15
#: 单次响应最大读取字节（页面 HTML / 图标都足够）
_URL_ICON_MAX_BYTES = 2 * 1024 * 1024
#: 图标缓存边长（>= 列表显示尺寸，避免放大后发虚）
_URL_ICON_CACHE_PX = 64

_URL_ICON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_LINK_REL_RE = re.compile(r'rel\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_LINK_HREF_RE = re.compile(r'<link\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _normalize_url(raw: str) -> str | None:
    """把用户输入的网址规范化为 http(s) 绝对 URL；无效返回 None。

    用 QUrl.fromUserInput 自动补协议头、识别 host:port，保证后续 urllib 可用。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    qurl = QtCore.QUrl.fromUserInput(raw)
    if not qurl.isValid() or qurl.scheme() not in ("http", "https"):
        return None
    return qurl.toString()


def _url_icon_key(url: str) -> str:
    """基于协议+主机计算 favicon 缓存 key（与路径/参数无关，稳定不重抓）。"""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or url).lower()
        scheme = (parts.scheme or "http").lower()
        key = f"{scheme}://{host}"
    except Exception:  # noqa: BLE001
        key = url
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def _http_get_bytes(url: str, timeout: int) -> tuple[bytes | None, str, bool]:
    """GET 请求，返回 ``(响应体, Content-Type, 是否成功连上服务器)``。

    - 正常/4xx/5xx 响应：``(data|None, ctype, True)``（已到达服务器）
    - 超时/连接失败/DNS：``(None, "", False)``
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _URL_ICON_USER_AGENT,
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(_URL_ICON_MAX_BYTES)
            ctype = resp.headers.get("Content-Type", "") or ""
            return data, ctype, True
    except urllib.error.HTTPError as e:
        # 服务器有响应（404/403/500...）→ 视为"已到达站点"
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
        return None, ctype, True
    except Exception:  # noqa: BLE001 - 超时/内网/无网等连接层错误
        return None, "", False


def _parse_icon_candidates(html: bytes, base_url: str) -> list[str]:
    """从网页 HTML 提取 favicon 链接（icon / shortcut icon / apple-touch-icon），
    相对地址用 base_url 转绝对。"""
    text = html.decode("utf-8", errors="ignore")
    found: list[str] = []
    for tag in _LINK_TAG_RE.findall(text):
        rel = _LINK_REL_RE.search(tag)
        if not rel:
            continue
        rels = {r.strip().lower() for r in rel.group(1).split()}
        if "icon" not in rels and "apple-touch-icon" not in rels:
            continue
        href = _LINK_HREF_RE.search(tag)
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, href.group(1).strip())
        if url.startswith(("http://", "https://")):
            found.append(url)
    return found


def _looks_like_image(data: bytes, content_type: str) -> bool:
    """粗略判断字节流是否为可渲染的图片（按魔数/Content-Type 判断）。"""
    if not data:
        return False
    ct = (content_type or "").lower()
    if "text" in ct or "html" in ct:
        return False
    head = data[:16]
    if (
        head.startswith(b"\x89PNG")
        or head.startswith(b"\xff\xd8")          # jpeg
        or head.startswith(b"GIF8")              # gif
        or head.startswith(b"RIFF")              # webp
        or (head.startswith(b"\x00\x00\x01\x00") and len(data) > 16)  # ico
        or data.lstrip().startswith(b"<svg")
    ):
        return True
    return ct.startswith("image/")


def _load_icon_pixmap(data: bytes, content_type: str = "") -> QtGui.QPixmap | None:
    """把 favicon 字节流加载为 QPixmap；SVG 走 QtSvg 栅格化，失败返回 None。"""
    if not data:
        return None
    img = QtGui.QImage()
    if img.loadFromData(data):
        pm = QtGui.QPixmap.fromImage(img)
        if not pm.isNull():
            return pm
    if "svg" in (content_type or "").lower() or data.lstrip().startswith(b"<svg"):
        try:
            from PySide6 import QtSvg
        except Exception:  # noqa: BLE001
            return None
        try:
            renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
            pm = QtGui.QPixmap(64, 64)
            pm.fill(QtCore.Qt.GlobalColor.transparent)
            painter = QtGui.QPainter(pm)
            renderer.render(painter)
            painter.end()
            if not pm.isNull():
                return pm
        except Exception:  # noqa: BLE001
            return None
    return None


def _fetch_url_favicon_worker(
    normalized_url: str,
) -> tuple[str | None, bytes | None, str, str]:
    """后台线程执行：访问网址一次，尝试取回站点图标。

    返回 ``(icon_url, icon_bytes, content_type, status)``：
    - status == "ok"             拿到图标
    - status == "no_icon"        正常访问到站点但没有可用图标（应 1 天不重试）
    - status == "network_error"  超时/连接失败（应尽快重试）
    纯 stdlib（urllib），不在线程里碰 QWidget。
    """
    parts = urllib.parse.urlsplit(normalized_url)
    root = f"{parts.scheme}://{parts.netloc}"
    reached = False

    # 1) 优先直接取站点根 favicon.ico（一次请求最快最稳，SPA 站点也适用）
    ico_url = f"{root}/favicon.ico"
    data, ctype, ok = _http_get_bytes(ico_url, _URL_ICON_TIMEOUT_SEC)
    if ok:
        reached = True
    if data and _looks_like_image(data, ctype):
        return ico_url, data, ctype, "ok"

    # 2) 回退：抓页面 HTML 解析 <link rel="icon">
    page, _ctype, ok2 = _http_get_bytes(normalized_url, _URL_ICON_TIMEOUT_SEC)
    if ok2:
        reached = True
    candidates: list[str] = []
    if page:
        candidates = _parse_icon_candidates(page, normalized_url)
    for icon_url in candidates:
        d, c, _ = _http_get_bytes(icon_url, _URL_ICON_TIMEOUT_SEC)
        if d and _looks_like_image(d, c):
            return icon_url, d, c, "ok"

    if not reached:
        return None, None, "", "network_error"
    return None, None, "", "no_icon"


class FolderFavoritesWidget(QtWidgets.QWidget):
    """文件夹收藏桌面组件（嵌入到 l_notepad）"""

    # Ctrl+中键识别到调用程序/地址栏路径后发出，用于更新自定义标题栏文本
    caller_info_changed = QtCore.Signal(str)
    # 剪贴板历史后台加载完成（携带 list[dict]），用于在主线程重建 model
    _clipboard_loaded_signal = QtCore.Signal(object)
    # 网址 favicon 后台抓取完成（携带 (key, url, icon_url, data, content_type)）
    _url_icon_done_signal = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None, restart_callback=None) -> None:
        super().__init__(parent)
        self._restart_callback = restart_callback  # 保存重启回调（兼容现有调用）
        self._last_clipboard_text = ""  # 上一次剪贴板内容
        # 上一次剪贴板内容签名（kind+key），用于图片/文件/文本统一去重防抖
        self._last_clipboard_key: tuple = ("", "")
        self._explorer_hwnd = None  # 收藏夹导航的资源管理器窗口句柄
        self._caller_program = ""    # Ctrl+中键唤起时的调用程序名（如 explorer.exe）
        self._caller_path = ""       # 调用者地址栏当前文件夹路径（仅 Explorer 可读）
        self._filter_index = 0       # 显示筛选：0=全部 1=文件夹 2=网址 3=命令
        self._favorites_kind = "folder"  # 收藏种类：folder=文件夹收藏(全部) / url=网址收藏(独立文件)
        self._ui_initialized = False
        self._clipboard_connected = False
        # 写盘防抖：剪贴板变化频繁时合并多次写入，避免阻塞 UI
        self._clipboard_save_timer = QtCore.QTimer(self)
        self._clipboard_save_timer.setSingleShot(True)
        self._clipboard_save_timer.setInterval(800)
        self._clipboard_save_timer.timeout.connect(self._save_clipboard_history)
        self._cloud_api = None  # NotepadApi（带登录 token）；None=未登录（云 item 隐藏）
        self._setup_data()
        # 列表行号 → 收藏 dict（真实对象）映射，随 _refresh_list 重建。
        # 不能用 item.data(UserRole)：PySide6 会经 QVariant 深拷贝 dict，
        # 取回的只是副本，无法按对象身份定位回 self.favorites。此映射在
        # 渲染时按“过滤后可见顺序”记录真实对象，供增删改直接命中。
        self._row_to_fav: dict[int, dict] = {}
        # 网址 favicon 缓存与后台抓取状态
        self._url_icons_dir = self.favorites_dir / _URL_ICONS_DIR_NAME
        self._url_icons_dir.mkdir(parents=True, exist_ok=True)
        self._url_icon_fetching: set[str] = set()   # 抓取中的 key
        self._url_icon_pending: dict[str, str] = {}  # 待抓取 key → 规范化 URL
        self._url_icon_schedule_timer = QtCore.QTimer(self)
        self._url_icon_schedule_timer.setSingleShot(True)
        self._url_icon_schedule_timer.setInterval(500)
        self._url_icon_schedule_timer.timeout.connect(self._start_pending_url_icon_fetches)
        self._url_icon_refresh_timer = QtCore.QTimer(self)
        self._url_icon_refresh_timer.setSingleShot(True)
        self._url_icon_refresh_timer.setInterval(200)
        self._url_icon_refresh_timer.timeout.connect(self._refresh_list)
        self._url_icon_done_signal.connect(self._on_url_icon_done)
        QtCore.QTimer.singleShot(0, self.finalize_ui)

    def _favorites_icon_size(self) -> int:
        """收藏列表项图标边长：网址收藏页用 favicon 本体尺寸（画布贴合）。"""
        if getattr(self, "_favorites_kind", "folder") == "url":
            return _FAV_ICON_SIZE_URL
        return _FAV_ICON_SIZE_COMPACT

    def _favorites_icon_tight(self) -> bool:
        """网址收藏页用贴合画布（无透明留白），行距才是设定值。"""
        return getattr(self, "_favorites_kind", "folder") == "url"

    def _favorites_item_height(self) -> int:
        """收藏列表项行高：网址页 = 图标边长 + 间距；紧凑页保持原行高。"""
        if self._favorites_icon_tight():
            return self._favorites_icon_size() + _FAV_ITEM_GAP_PX
        return _FAV_ITEM_HEIGHT_COMPACT

    def _apply_favorites_list_compact_style(self) -> None:
        """压缩收藏夹列表项间距，让路径列表更密集。

        网址收藏页展示的是站点 favicon（图标需醒目），按紧凑尺寸的 3 倍放大，
        并抬高行高避免图标被裁切；其它收藏种类保持紧凑。
        """
        icon_px = self._favorites_icon_size()
        obj_name = self.list_widget.objectName() or "folder_favorites_list"
        self.list_widget.setSpacing(0)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setIconSize(QtCore.QSize(icon_px, icon_px))
        self.list_widget.setStyleSheet(
            f"""
            QListWidget#{obj_name} {{
                padding: 1px 2px;
                outline: none;
            }}
            QListWidget#{obj_name}::item {{
                min-height: {self._favorites_item_height()}px;
                padding: 0px 1px;
                margin: 0;
                font-size: 18px;
            }}
            QListWidget#{obj_name}::item:selected {{
                border-radius: 4px;
            }}
            """
        )

    def finalize_ui(self) -> None:
        if self._ui_initialized:
            return
        self._ui_initialized = True
        self._setup_ui()
        self._refresh_list()
        self._refresh_clipboard_display()
        clipboard = QtWidgets.QApplication.clipboard()
        if not self._clipboard_connected:
            clipboard.dataChanged.connect(self._on_clipboard_changed)
            self._clipboard_connected = True

    # ── 云同步（收藏项存数据库，仅登录后可用）──
    def set_cloud_api(self, api) -> None:
        """设置云同步 API（NotepadApi，带登录 token）；None=登出（云 item 隐藏）。"""
        self._cloud_api = api
        if self._cloud_ready():
            self._load_cloud_items()
        self._refresh_list()

    def _cloud_ready(self) -> bool:
        return self._cloud_api is not None and bool(getattr(self._cloud_api, "token", ""))

    def _load_cloud_items(self) -> None:
        """从后端拉取当前登录用户的云收藏，与本地收藏合并显示。"""
        try:
            items = self._cloud_api.list_fav_items(self._favorites_kind)
        except Exception as exc:  # noqa: BLE001
            lprint(f"[收藏云同步] 拉取云端收藏失败: {exc}")
            return
        local_items = [f for f in self.favorites if not f.get("cloud")]
        clouds = [self._fav_from_cloud(it) for it in items]
        self.favorites = clouds + local_items

    @staticmethod
    def _fav_from_cloud(it: dict) -> dict:
        kind = str(it.get("kind") or "folder")
        name = str(it.get("name") or "")
        value = str(it.get("value") or "")
        fav: dict = {"cloud": True, "id": it.get("id")}
        if kind == "url":
            fav.update({"type": "url", "name": name, "url": value})
        elif kind == "command":
            fav.update({"type": "command", "name": name, "command": value})
        else:
            fav.update({"type": "folder", "name": name, "path": value})
        return fav

    def _fav_cloud_payload(self, fav: dict) -> dict:
        return FavoriteEntry.from_dict(fav).cloud_payload(
            fallback_kind=self._favorites_kind
        )

    def _toggle_cloud(self, fav: dict) -> None:
        """右键切换：本地 ↔ 云同步。"""
        if not isinstance(fav, dict):
            return
        if not self._cloud_ready():
            QtWidgets.QMessageBox.information(self, "提示", "请先登录后再使用云同步")
            return
        try:
            if fav.get("cloud"):
                if fav.get("id"):
                    self._cloud_api.delete_fav_item(fav["id"])
                fav["cloud"] = False
                fav.pop("id", None)
            else:
                item = self._cloud_api.add_fav_item(**self._fav_cloud_payload(fav))
                if item and item.get("id"):
                    fav["id"] = item["id"]
                fav["cloud"] = True
        except Exception as exc:  # noqa: BLE001
            lprint(f"[收藏云同步] 切换失败: {exc}")
            QtWidgets.QMessageBox.warning(self, "云同步失败", str(exc))
            return
        self._save_favorites()
        self._refresh_list()

    def _sync_all_local_to_cloud(self) -> None:
        """一次性把当前所有「本地项」上传到数据库（云同步）。

        对每个 cloud != True 的收藏项调用 add_fav_item，成功则标记 cloud=True
        并写回后端返回的 id。单项失败不中断整体，最后汇总成功/失败数量。
        已经是云项的不会重复上传。
        """
        if not self._cloud_ready():
            QtWidgets.QMessageBox.information(self, "提示", "请先登录后再使用云同步")
            return

        local_items = [f for f in self.favorites if not f.get("cloud")]
        if not local_items:
            QtWidgets.QMessageBox.information(self, "提示", "没有需要同步的本地收藏（均已在云端）")
            return

        reply = QtWidgets.QMessageBox.question(
            self,
            "同步到数据库",
            f"将把 {len(local_items)} 个本地收藏上传到数据库并转为云同步项，确定继续？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        ok = 0
        failed = 0
        first_error = ""
        for fav in local_items:
            try:
                item = self._cloud_api.add_fav_item(**self._fav_cloud_payload(fav))
                if item and item.get("id"):
                    fav["id"] = item["id"]
                fav["cloud"] = True
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if not first_error:
                    first_error = str(exc)
                lprint(f"[收藏云同步] 上传失败: {exc}")

        self._save_favorites()
        self._refresh_list()

        if failed == 0:
            QtWidgets.QMessageBox.information(self, "同步完成", f"已成功同步 {ok} 个收藏到数据库")
        else:
            QtWidgets.QMessageBox.warning(
                self, "同步完成（部分失败）",
                f"成功 {ok} 个，失败 {failed} 个。\n首个错误：{first_error}",
            )

    def set_caller_hwnd(self, hwnd: int) -> None:
        """设置快捷键触发时的前台窗口句柄（由 ui.py 调用）。

        同时识别调用程序名，并在调用者是资源管理器时读取其地址栏当前路径。
        """
        self._explorer_hwnd = hwnd
        self._caller_program = self._get_process_name(hwnd)
        self._caller_path = self._get_explorer_path_for_hwnd(hwnd)
        lprint(
            f"收藏夹已记录调用者: hwnd={hwnd}, program={self._caller_program!r}, "
            f"path={self._caller_path!r}"
        )
        self._update_caller_info_label()

    def _get_process_name(self, hwnd: int) -> str:
        """根据窗口句柄取所属进程的可执行文件名（如 explorer.exe）。"""
        try:
            import ctypes
            from ctypes import wintypes

            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return ""
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if not handle:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    return os.path.basename(buf.value)
            finally:
                kernel32.CloseHandle(handle)
        except Exception as e:
            lprint(f"识别调用程序失败: {e}")
        return ""

    def _get_explorer_path_for_hwnd(self, hwnd: int) -> str:
        """若调用者是资源管理器窗口，读取其地址栏当前文件夹路径（Shell COM）。"""
        try:
            import pythoncom
            import win32com.client
        except Exception as e:
            lprint(f"读取地址栏路径所需组件不可用: {e}")
            return ""

        path = ""
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            for window in shell.Windows():
                try:
                    if int(window.HWND) != int(hwnd):
                        continue
                    folder = window.Document.Folder
                    path = str(folder.Self.Path)
                    break
                except Exception:
                    continue
        except Exception as e:
            lprint(f"读取 Explorer 地址栏路径失败: {e}")
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

        if path and os.path.isdir(path):
            return os.path.normpath(path)
        return ""

    def _update_caller_info_label(self) -> None:
        """发出识别到的调用者信息，由外层更新到自定义标题栏。"""
        program = self._caller_program or "未知程序"
        if self._caller_path:
            text = f" {program} — {self._caller_path}"
        else:
            text = f" {program}（未识别到地址栏路径）"
        self.caller_info_changed.emit(text)

    def show_actions_menu(self, global_pos) -> None:
        """在「文件夹收藏」标签右键时弹出操作菜单。"""
        kind = self._favorites_kind
        menu = QtWidgets.QMenu(self)

        if kind == "command":
            menu.addAction(" 添加命令").triggered.connect(self._add_command)
            menu.addSeparator()
            menu.addAction(" 执行").triggered.connect(self._execute_item)
            menu.addAction(" 编辑").triggered.connect(
                lambda: self._rename_item(self._current_fav()))
            menu.addAction(" 删除").triggered.connect(self._remove_item)
        elif kind == "url":
            menu.addAction(" 添加网址").triggered.connect(self._add_url)
            menu.addSeparator()
            menu.addAction(" 打开").triggered.connect(self._execute_item)
            menu.addAction(" 编辑").triggered.connect(
                lambda: self._rename_item(self._current_fav()))
            menu.addAction(" 删除").triggered.connect(self._remove_item)
        else:
            menu.addAction(" 添加当前文件夹").triggered.connect(self._add_current_folder)
            menu.addAction(" 浏览添加文件夹").triggered.connect(self._browse_add_folder)
            menu.addSeparator()
            menu.addAction(" 打开").triggered.connect(self._execute_item)
            menu.addAction(" 编辑").triggered.connect(
                lambda: self._rename_item(self._current_fav()))
            menu.addAction(" 删除").triggered.connect(self._remove_item)

        # 云同步：把当前所有本地项一次性上传到数据库（仅登录后可用）
        if self._cloud_ready():
            menu.addSeparator()
            menu.addAction("☁ 全部同步到数据库").triggered.connect(
                self._sync_all_local_to_cloud
            )

        menu.exec(global_pos)

    def _set_filter_index(self, index: int) -> None:
        self._filter_index = int(index)
        self._refresh_list()

    def _setup_data(self) -> None:
        """初始化数据路径"""
        self.favorites_dir = paths.favorites_dir()
        self.favorites_file = self.favorites_dir / self._favorites_filename()
        self.favorites: list[FavoriteItem] = self._load_favorites()
        # 剪贴板历史：先放占位空 model，真实数据由后台线程加载后回主线程重建
        # （2000 条 JSON 解析不占主线程；model 是 QObject 不能跨线程创建）
        self._clipboard_file = self.favorites_dir / "clipboard_history.json"
        self.clipboard_model = ClipboardHistoryModel([])
        self._clipboard_loaded_signal.connect(self._on_clipboard_history_loaded)
        self._load_clipboard_history_async()
        # 空闲时预热剪贴板弹窗：把首次显示的大列表布局成本移出 Win+V 热键路径
        QtCore.QTimer.singleShot(2000, self._prewarm_clipboard_popup)

    def _favorites_filename(self) -> str:
        """按收藏种类返回数据文件名。"""
        if self._favorites_kind == "url":
            return "url_favorites.json"
        if self._favorites_kind == "command":
            return "command_favorites.json"
        return "folder_favorites.json"

    def set_favorites_kind(self, kind: str) -> None:
        """设置收藏种类（folder/url/command）。

        需在面板创建后、由外部（ui.py）调用；会重新指向数据文件并刷新。
        command 类型首次加载时自动从旧 favorites.json 迁移命令条目。
        """
        if kind == self._favorites_kind:
            return
        self._favorites_kind = kind
        self.favorites_file = self.favorites_dir / self._favorites_filename()
        self.favorites = self._load_favorites()
        if kind == "url":
            self._filter_index = 2  # 仅显示网址
        elif kind == "command":
            self._filter_index = 3  # 仅显示命令
            self._migrate_commands_from_legacy()
        if getattr(self, "_ui_initialized", False) and getattr(self, "list_widget", None) is not None:
            self._apply_favorites_list_compact_style()  # 图标尺寸随种类变化（网址页放大）
            self._refresh_list()

    def _migrate_commands_from_legacy(self) -> None:
        """从 favorites.json 中提取 command 条目合并到 command_favorites.json。

        每次启动都检查，确保文件夹收藏中残留的命令也被搬走。
        """
        legacy_file = self.favorites_dir / "favorites.json"
        if not legacy_file.exists():
            return
        try:
            with open(legacy_file, "r", encoding="utf-8") as f:
                legacy_items = json.load(f)
        except Exception:
            return
        if not isinstance(legacy_items, list):
            return
        commands_in_legacy = [
            item for item in legacy_items
            if isinstance(item, dict) and (
                item.get("type") == "command" or item.get("command")
            )
        ]
        if not commands_in_legacy:
            return
        # 合并：已有 command_favorites.json 数据 + 旧文件中的命令（去重）
        existing_keys = {
            (f.get("name", ""), f.get("command", ""))
            for f in self.favorites if isinstance(f, dict)
        }
        merged = list(self.favorites)
        for cmd in commands_in_legacy:
            key = (cmd.get("name", ""), cmd.get("command", ""))
            if key not in existing_keys:
                merged.append(cmd)
                existing_keys.add(key)
        self.favorites = merged
        self._save_favorites()
        # 从旧文件移除 command 条目
        remaining = [
            item for item in legacy_items
            if not (isinstance(item, dict) and (
                item.get("type") == "command" or item.get("command")
            ))
        ]
        try:
            with open(legacy_file, "w", encoding="utf-8") as f:
                json.dump(remaining, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        lprint(f"[命令收藏] 从 favorites.json 迁移了 {len(commands_in_legacy)} 条命令")

    def _load_favorites(self) -> list[FavoriteItem]:
        """加载收藏夹数据"""
        if self.favorites_file.exists():
            try:
                with open(self.favorites_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                lprint(f"加载收藏夹失败: {e}")
        return []

    def _save_favorites(self) -> None:
        """保存收藏夹数据"""
        try:
            with open(self.favorites_file, "w", encoding="utf-8") as f:
                json.dump(self.favorites, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "错误", f"保存收藏夹失败: {e}")

    def _read_clipboard_history(self) -> list[dict]:
        """读取剪贴板历史（纯 IO+JSON，可在后台线程调用；不建 QObject）。"""
        if self._clipboard_file.exists():
            try:
                with open(self._clipboard_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        items: list[dict] = []
                        for it in data:
                            if not isinstance(it, dict):
                                continue
                            it = dict(it)
                            if "kind" not in it:
                                it["kind"] = "text"
                            items.append(it)
                        return items[:CLIPBOARD_MAX_STORED]
            except Exception as e:
                lprint(f"加载剪贴板历史失败: {e}")
        return []

    def _load_clipboard_history_async(self) -> None:
        """后台线程加载剪贴板历史，完成后回主线程重建 model（不阻塞启动）。"""
        import threading

        def _work() -> None:
            items = self._read_clipboard_history()
            self._clipboard_loaded_signal.emit(items)

        threading.Thread(target=_work, daemon=True).start()

    @QtCore.Slot(object)
    def _on_clipboard_history_loaded(self, items: list) -> None:
        """后台加载完成：在主线程重建 model（QObject 必须留在主线程）。"""
        if not isinstance(items, list):
            return
        existing = self.clipboard_model.items() if self.clipboard_model else []
        # 加载期间可能已有新剪贴板项写入占位空 model（概率极低），新项置前
        if existing:
            items = existing + items
        self.clipboard_model = ClipboardHistoryModel(items)
        # model 已被替换，必须重新绑定代理/列表；否则 QListView 仍显示旧的
        # 空占位 model，导致剪贴板历史页面一直为空（只会在 count 标签出数字）。
        proxy = getattr(self, "_clipboard_proxy", None)
        if proxy is not None:
            proxy.setSourceModel(self.clipboard_model)
        self._update_clipboard_count_label()

    def _save_clipboard_history(self) -> None:
        """保存剪贴板历史记录（由防抖 timer 触发，避免频繁同步写盘）"""
        try:
            with open(self._clipboard_file, "w", encoding="utf-8") as f:
                json.dump(
                    self.clipboard_model.items(), f, ensure_ascii=False, indent=2
                )
        except Exception as e:
            lprint(f"保存剪贴板历史失败: {e}")

    def _schedule_save_clipboard(self) -> None:
        """请求一次写盘（防抖：800ms 内的多次请求合并为一次）。"""
        self._clipboard_save_timer.start()

    def _setup_ui(self) -> None:
        """初始化UI。优先复用 main_window.ui 中定义的控件。"""
        if self.layout() is not None:
            # 兼容多种 .ui 变体：文件夹/网址/命令收藏各有独立 list widget
            fav_list = (
                self.findChild(QtWidgets.QListWidget, "folder_favorites_list")
                or self.findChild(QtWidgets.QListWidget, "url_favorites_list")
                or self.findChild(QtWidgets.QListWidget, "command_favorites_list")
            )
            cb_count = self.findChild(QtWidgets.QLabel, "clipboard_count_label")
            cb_btn = self.findChild(QtWidgets.QToolButton, "ClipboardActionsButton")
            cb_search = self.findChild(QtWidgets.QLineEdit, "clipboard_search")
            cb_list = self.findChild(QtWidgets.QListView, "clipboard_list")
            # 完整变体：收藏列表 + 剪贴板区域齐全
            if fav_list is not None and all([cb_count, cb_btn, cb_search, cb_list]):
                self.list_widget = fav_list
                self.clipboard_count_label = cb_count
                self.clipboard_actions_btn = cb_btn
                self._clipboard_search = cb_search
                self.clipboard_list = cb_list
                self._setup_existing_ui_widgets()
                return
            # 精简变体（网址收藏页）：只有收藏列表，无剪贴板控件
            if fav_list is not None:
                self.list_widget = fav_list
                self._setup_existing_favorites_list_only()
                return

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # 标题
        title_label = QtWidgets.QLabel(" 文件夹收藏与命令")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 5px;")
        layout.addWidget(title_label)

        # 收藏夹列表
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setObjectName("folder_favorites_list")
        self._apply_favorites_list_compact_style()
        self.list_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.itemDoubleClicked.connect(self._execute_item)
        # Shift 连续范围多选 / Ctrl 逐个切换选中（Qt 标准扩展选择）
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        # 启用拖拽排序
        self.list_widget.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list_widget.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.viewport().setAcceptDrops(True)
        # 监听拖拽完成事件，保存新顺序
        self.list_widget.model().rowsMoved.connect(self._on_items_reordered)
        layout.addWidget(self.list_widget)

        # 操作按钮与显示筛选已合并到「文件夹收藏」标签的右键菜单，见 show_actions_menu()

        # 说明标签
        hint_label = QtWidgets.QLabel(
            "提示: 右键「文件夹收藏」标签可添加/执行/删除并切换显示类型；"
            "名称与路径支持 {y}{m}{d} {wd} {hh}{mi}{ss} {pc} {user} 等变量"
        )
        hint_label.setToolTip(fav_vars.VARIABLE_HELP)
        hint_label.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(hint_label)

        # ===== 剪贴板历史区域 =====
        clipboard_group = QtWidgets.QWidget()
        clipboard_group.setObjectName("ClipboardGroup")
        clipboard_group.setStyleSheet(
            "#ClipboardGroup { background-color: #2b2b2b; border-radius: 5px; padding: 5px; }"
        )
        clipboard_main_layout = QtWidgets.QVBoxLayout(clipboard_group)

        # 标题行（标题 + 清除按钮）
        clipboard_title_row = QtWidgets.QHBoxLayout()
        clipboard_title = QtWidgets.QLabel(" 剪贴板历史")
        clipboard_title.setStyleSheet(
            "color: #cccccc; font-size: 12px; font-weight: bold; padding: 3px;"
        )
        clipboard_title_row.addWidget(clipboard_title)

        self.clipboard_count_label = QtWidgets.QLabel(
            f"{self.clipboard_model.rowCount()} 条"
        )
        self.clipboard_count_label.setObjectName("clipboard_count_label")
        self.clipboard_count_label.setStyleSheet(
            "color: #89DDFF; font-size: 12px; padding: 3px 6px;"
        )
        clipboard_title_row.addWidget(self.clipboard_count_label)
        clipboard_title_row.addStretch()

        # 三个操作（刷新/清理重复/清除全部）收进一个「箭头」下拉按钮。
        self.clipboard_actions_btn = QtWidgets.QToolButton()
        self.clipboard_actions_btn.setObjectName("ClipboardActionsButton")
        self.clipboard_actions_btn.setText("")
        self.clipboard_actions_btn.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.clipboard_actions_btn.setFixedHeight(10)
        self.clipboard_actions_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.clipboard_actions_btn.setStyleSheet(
            """
            QToolButton#ClipboardActionsButton {
                padding: 0 6px;
                font-size: 10px;
                line-height: 10px;
                border: 1px solid rgba(137, 221, 255, 0.25);
                border-radius: 5px;
                background: rgba(255,255,255,0.04);
                color: #89DDFF;
                margin: 0 4px;
            }
            QToolButton#ClipboardActionsButton:hover {
                background: rgba(137, 221, 255, 0.15);
            }
            QToolButton#ClipboardActionsButton::menu-indicator {
                image: none;
                width: 0;
            }
            QMenu#ClipboardActionsMenu::item {
                height: 20px;
                padding: 0 16px;
                font-size: 12px;
            }
            """
        )

        clipboard_menu = QtWidgets.QMenu(self.clipboard_actions_btn)
        clipboard_menu.setObjectName("ClipboardActionsMenu")
        act_refresh = clipboard_menu.addAction(" 刷新")
        act_refresh.triggered.connect(self._refresh_clipboard_display)
        act_dedupe = clipboard_menu.addAction(" 清理重复")
        act_dedupe.triggered.connect(self._dedupe_clipboard_history)
        act_clear = clipboard_menu.addAction(" 清除全部")
        act_clear.triggered.connect(self._clear_clipboard_history)
        self.clipboard_actions_btn.setMenu(clipboard_menu)
        clipboard_title_row.addWidget(self.clipboard_actions_btn)
        clipboard_main_layout.addLayout(clipboard_title_row)

        # 搜索过滤
        self._clipboard_search = QtWidgets.QLineEdit()
        self._clipboard_search.setObjectName("clipboard_search")
        self._clipboard_search.setPlaceholderText(" 搜索剪贴板历史...")
        self._clipboard_search.setClearButtonEnabled(True)
        self._clipboard_search.setStyleSheet(
            "background-color: #1e1e1e; border: 1px solid #3c3c3c; color: #d4d4d4;"
            " border-radius: 4px; padding: 4px 6px;"
        )
        self._clipboard_search.textChanged.connect(self._on_clipboard_search_changed)
        clipboard_main_layout.addWidget(self._clipboard_search)

        # 剪贴板列表（QListView + Model 虚拟化：只渲染可见行，海量历史也不卡）
        self._clipboard_proxy = QtCore.QSortFilterProxyModel(self)
        self._clipboard_proxy.setSourceModel(self.clipboard_model)
        self._clipboard_proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self._clipboard_proxy.setFilterRole(ClipboardHistoryModel.SearchRole)

        self.clipboard_list = QtWidgets.QListView()
        self.clipboard_list.setObjectName("clipboard_list")
        self.clipboard_list.setModel(self._clipboard_proxy)
        # 文本/图片/文件行高不同，关闭 uniformItemSizes 让 delegate 按需计算行高
        self.clipboard_list.setUniformItemSizes(False)
        self.clipboard_list.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.clipboard_list.setSpacing(2)  # 降低 item 间隔
        self.clipboard_list.setStyleSheet(
            """
            QListView#clipboard_list {
                background-color: #1e1e1e;
                border: 1px solid #3c3c3c;
                color: #d4d4d4;
                padding: 2px;
            }
            QListView#clipboard_list::item {
                padding: 1px 1px;
                margin: 0;
                min-height: 16px;
            }
            QListView#clipboard_list::item:selected {
                background-color: #3a3a3a;
                border-radius: 3px;
            }
            QScrollBar:vertical {
                background: #1e1e1e;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #5a5a5a;
                min-height: 24px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover {
                background: #6e6e6e;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )
        self.clipboard_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.clipboard_list.customContextMenuRequested.connect(
            self._show_clipboard_context_menu
        )
        self.clipboard_list.doubleClicked.connect(self._use_clipboard_item)

        # 文本/图片/文件混合渲染（图片缩略图、文件图标、紧凑文本行）
        self.clipboard_list.setItemDelegate(ClipboardItemDelegate(self))
        if not getattr(self, "_clipboard_hover_preview", None):
            self._clipboard_hover_preview = ImageHoverPreview(self.clipboard_list)
        clipboard_main_layout.addWidget(self.clipboard_list)

        layout.addWidget(clipboard_group)

    def _wire_favorites_list(self) -> None:
        """收藏列表的通用初始化（样式、右键菜单、双击、拖拽排序）。"""
        self._apply_favorites_list_compact_style()
        self.list_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.itemDoubleClicked.connect(self._execute_item)
        # Shift 连续范围多选 / Ctrl 逐个切换选中（Qt 标准扩展选择）
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.list_widget.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.list_widget.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.viewport().setAcceptDrops(True)
        self.list_widget.model().rowsMoved.connect(self._on_items_reordered)

    def _setup_existing_favorites_list_only(self) -> None:
        """精简变体（网址收藏页）：仅初始化收藏列表，无剪贴板区域。"""
        self._wire_favorites_list()

    def _setup_existing_ui_widgets(self) -> None:
        self._wire_favorites_list()

        self.clipboard_count_label.setText(f"{self.clipboard_model.rowCount()} 条")
        self.clipboard_actions_btn.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.clipboard_actions_btn.setCursor(QtCore.Qt.PointingHandCursor)

        clipboard_menu = QtWidgets.QMenu(self.clipboard_actions_btn)
        clipboard_menu.setObjectName("ClipboardActionsMenu")
        act_refresh = clipboard_menu.addAction(" 刷新")
        act_refresh.triggered.connect(self._refresh_clipboard_display)
        act_dedupe = clipboard_menu.addAction(" 清理重复")
        act_dedupe.triggered.connect(self._dedupe_clipboard_history)
        act_clear = clipboard_menu.addAction(" 清除全部")
        act_clear.triggered.connect(self._clear_clipboard_history)
        self.clipboard_actions_btn.setMenu(clipboard_menu)

        self._clipboard_search.textChanged.connect(self._on_clipboard_search_changed)
        self._clipboard_proxy = QtCore.QSortFilterProxyModel(self)
        self._clipboard_proxy.setSourceModel(self.clipboard_model)
        self._clipboard_proxy.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        self._clipboard_proxy.setFilterRole(ClipboardHistoryModel.SearchRole)
        self.clipboard_list.setModel(self._clipboard_proxy)
        # 文本/图片/文件行高不同，关闭 uniformItemSizes 让 delegate 按需计算行高
        self.clipboard_list.setUniformItemSizes(False)
        self.clipboard_list.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.clipboard_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.clipboard_list.customContextMenuRequested.connect(
            self._show_clipboard_context_menu
        )
        self.clipboard_list.doubleClicked.connect(self._use_clipboard_item)

        # 文本/图片/文件混合渲染（图片缩略图、文件图标、紧凑文本行）
        self.clipboard_list.setItemDelegate(ClipboardItemDelegate(self))
        if not getattr(self, "_clipboard_hover_preview", None):
            self._clipboard_hover_preview = ImageHoverPreview(self.clipboard_list)

    def _refresh_list(self) -> None:
        """刷新列表"""
        self.list_widget.clear()
        self._row_to_fav = {}
        filter_type = getattr(self, "_filter_index", 0)
    
        # 创建图标缓存
        folder_icon = self.style().standardIcon(QtWidgets.QStyle.SP_DirIcon)
        command_icon = self.style().standardIcon(QtWidgets.QStyle.SP_CommandLink)
    
        for fav in self.favorites:
            # 云 item 未登录时隐藏（数据在数据库，仅登录用户可见）
            if fav.get("cloud") and not self._cloud_ready():
                continue
            item_type = fav.get("type", "")
            if not item_type:
                # 兼容旧数据 / 跨标签粘贴来的其它类型条目：按现有字段推断
                if fav.get("path"):
                    item_type = "folder"
                elif fav.get("url"):
                    item_type = "url"
                elif fav.get("command"):
                    item_type = "command"
                else:
                    item_type = "other"
    
            # 按标签种类强制过滤：文件夹标签不显示命令，命令标签只显示命令
            kind = self._favorites_kind
            if kind == "folder" and item_type == "command":
                continue
            if kind == "command" and item_type != "command":
                continue
            if kind == "url" and item_type != "url":
                continue
            # 用户手动筛选（仅在文件夹标签内生效）
            if kind == "folder":
                if filter_type == 1 and item_type != "folder":
                    continue
                if filter_type == 2 and item_type != "url":
                    continue
    
            # 创建列表项（名称/路径等按内置变量实时展开；存盘仍是模板原文）
            entry = FavoriteEntry.from_dict(fav)
            disp_name = entry.expanded_name
            if item_type == "folder":
                display_text = f" {entry.expanded_value}"
                icon = folder_icon
            elif item_type == "url":
                disp_val = entry.expanded_value
                display_text = f" {disp_name}  —  {disp_val}" if disp_val else f" {disp_name}"
                icon = self._url_icon_for(disp_val)
                self._maybe_schedule_url_icon(disp_val)
            elif item_type == "command":
                display_text = f" {disp_name or entry.expanded_value}"
                icon = command_icon
            else:
                # 跨标签粘贴来的异类条目（如账号）：尽量显示名称
                display_text = f" {disp_name or fav.get('username', '') or '条目'}"
                icon = QtGui.QIcon()
    
            item = QtWidgets.QListWidgetItem(
                _icon_with_cloud(
                    icon,
                    bool(fav.get("cloud")),
                    self._favorites_icon_size(),
                    tight=self._favorites_icon_tight(),
                ),
                display_text,
            )
            item.setSizeHint(QtCore.QSize(0, self._favorites_item_height()))
            item.setData(QtCore.Qt.UserRole, fav)
            self.list_widget.addItem(item)
            self._row_to_fav[self.list_widget.count() - 1] = fav

    # ── 网址 favicon：读取缓存 / 后台抓取 ──
    def _url_icon_path(self, key: str) -> Path:
        return self._url_icons_dir / f"{key}.png"

    def _url_icon_fail_marker(self, key: str) -> Path:
        return self._url_icons_dir / f"{key}.fail"

    def _url_icon_for(self, url: str) -> QtGui.QIcon:
        """返回网址对应的 favicon；无缓存时回退默认「链接」图标。"""
        if url:
            path = self._url_icon_path(_url_icon_key(url))
            if path.exists():
                return QtGui.QIcon(str(path))
        return self.style().standardIcon(QtWidgets.QStyle.SP_FileLinkIcon)

    def _maybe_schedule_url_icon(self, url: str) -> None:
        """网址项缺缓存图标时登记待抓取（防抖合并；失败标记按原因分 TTL 重试）。"""
        normalized = _normalize_url(url)
        if not normalized:
            return
        key = _url_icon_key(normalized)
        if key in self._url_icon_fetching or key in self._url_icon_pending:
            return
        if self._url_icon_path(key).exists():
            return
        marker = self._url_icon_fail_marker(key)
        if marker.exists():
            if not self._url_icon_marker_expired(marker):
                return
        self._url_icon_pending[key] = normalized
        self._url_icon_schedule_timer.start()

    @staticmethod
    def _url_icon_marker_expired(marker: Path) -> bool:
        """判断失败标记是否已过 TTL。兼容旧格式（纯时间戳字符串，按 no_icon 处理）。"""
        try:
            raw = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return True
        ts: float = 0.0
        status = ""
        try:
            meta = json.loads(raw)
            if isinstance(meta, dict):
                ts = float(meta.get("t", 0))
                status = str(meta.get("s", ""))
            else:
                ts = float(raw)
        except Exception:  # noqa: BLE001 - 解析失败视为过期，允许重试
            return True
        ttl = (
            _URL_ICON_NET_ERR_TTL_SEC
            if status == "network_error"
            else _URL_ICON_FAIL_TTL_SEC
        )
        return (time.time() - ts) >= ttl

    def _start_pending_url_icon_fetches(self) -> None:
        """把待抓取队列派发到后台线程（限制并发数，完成回调继续排空）。"""
        while self._url_icon_pending:
            if len(self._url_icon_fetching) >= _URL_ICON_MAX_CONCURRENT:
                return
            key, url = next(iter(self._url_icon_pending.items()))
            del self._url_icon_pending[key]
            if key in self._url_icon_fetching or self._url_icon_path(key).exists():
                continue
            self._url_icon_fetching.add(key)
            threading.Thread(
                target=self._url_icon_fetch_worker_entry, args=(key, url), daemon=True
            ).start()

    def _url_icon_fetch_worker_entry(self, key: str, url: str) -> None:
        """后台线程入口：访问网址取图标，结果回主线程处理。"""
        icon_url, data, ctype, status = _fetch_url_favicon_worker(url)
        self._url_icon_done_signal.emit((key, url, icon_url, data, ctype, status))

    @QtCore.Slot(object)
    def _on_url_icon_done(self, payload: tuple) -> None:
        """主线程处理抓取结果：转换缓存 PNG 或按原因写失败标记，再刷新列表。"""
        key, url, _icon_url, data, ctype, status = payload
        self._url_icon_fetching.discard(key)
        path = self._url_icon_path(key)
        marker = self._url_icon_fail_marker(key)
        if data:
            pm = _load_icon_pixmap(data, ctype or "")
            if pm is not None and not pm.isNull():
                pm = pm.scaled(
                    _URL_ICON_CACHE_PX, _URL_ICON_CACHE_PX,
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
                if pm.save(str(path), "PNG"):
                    try:
                        marker.unlink(missing_ok=True)
                    except OSError:
                        pass
                    lprint(f"[网址收藏] 已获取网页图标: {url} -> {path.name}")
                    self._url_icon_refresh_timer.start()
                    self._start_pending_url_icon_fetches()
                    return
        # 抓取失败：写失败标记（含时间与原因，按原因分 TTL 重试）
        try:
            marker.write_text(
                json.dumps({"t": time.time(), "s": status or "no_icon"}),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._start_pending_url_icon_fetches()

    def _on_items_reordered(self) -> None:
        """拖拽排序完成后，更新 self.favorites 的顺序并保存"""
        # 从 UI 列表重建 favorites 顺序
        new_favorites = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            fav = item.data(QtCore.Qt.UserRole)
            if fav:
                new_favorites.append(fav)
        
        # 更新数据并保存
        self.favorites = new_favorites
        self._save_favorites()
        lprint(f" 已保存收藏顺序（{len(new_favorites)} 项）")

    def _add_current_folder(self) -> None:
        """添加当前文件夹：优先使用 Ctrl+中键唤起时识别到的调用者地址栏路径，
        识别不到再回退到手动选择对话框。"""
        if self._caller_path and os.path.isdir(self._caller_path):
            self._add_folder_by_path(self._caller_path)
            return
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self._add_folder_by_path(folder)

    def _browse_add_folder(self) -> None:
        """浏览并添加文件夹"""
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self._add_folder_by_path(folder)

    def _add_folder_by_path(self, path: str) -> None:
        """通过路径添加文件夹"""
        if not path:
            QtWidgets.QMessageBox.warning(self, "警告", "未能识别有效的文件夹路径")
            return
        if not os.path.exists(path):
            QtWidgets.QMessageBox.warning(self, "警告", f"路径不存在: {path}")
            return

        path = path.strip()
        if len(path) >= 2 and path[1] == ":":
            drive = path[0].upper()
            suffix = path[2:].replace("/", "\\").strip("\\")
            if not suffix:
                path = f"{drive}:\\"
                folder_name = f"{drive}:"
            else:
                path = os.path.normpath(path)
                folder_name = os.path.basename(path)
        else:
            path = os.path.abspath(path)
            folder_name = os.path.basename(path)

        # 检查是否已存在
        for fav in self.favorites:
            if fav.get("path") == path:
                QtWidgets.QMessageBox.information(self, "提示", "该文件夹已在收藏中")
                return

        self.favorites.append({"type": "folder", "name": folder_name, "path": path})
        self._save_favorites()
        self._refresh_list()

    def _add_url(self) -> None:
        """添加网址"""
        # 创建自定义对话框，包含名称和网址两个输入框
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("添加网址")
        dialog.setModal(True)
        dialog.setMinimumWidth(450)
        
        layout = QtWidgets.QVBoxLayout(dialog)
        
        # 名称输入
        name_label = QtWidgets.QLabel("网址名称:")
        name_input = QtWidgets.QLineEdit()
        name_input.setPlaceholderText("例如：GitHub、百度、公司内部系统")
        layout.addWidget(name_label)
        layout.addWidget(name_input)
        
        # 网址链接输入
        url_label = QtWidgets.QLabel("网址链接:")
        url_input = QtWidgets.QLineEdit()
        url_input.setPlaceholderText("例如：https://github.com/{date:%Y%m%d}")
        layout.addWidget(url_label)
        layout.addWidget(url_input)

        attach_var_preview(layout, [("名称", name_input), ("网址", url_input)])
        
        # 按钮
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        # 显示对话框
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            name = name_input.text().strip()
            url = url_input.text().strip()
            
            if not name or not url:
                return
            
            # 添加到收藏夹
            self.favorites.append({
                "type": "url",
                "name": name,
                "url": url
            })
            self._save_favorites()
            self._refresh_list()

    def _current_fav(self) -> dict | None:
        """当前列表选中项的收藏 dict（真实对象）。

        列表可能被筛选（如「全部」标签里隐藏命令、或按类型过滤），导致
        list_widget 的行号与 self.favorites 的下标不一致；且 item.data(UserRole)
        返回的是 QVariant 深拷贝的副本，无法按对象身份定位回 self.favorites。
        这里用 _refresh_list 渲染时记录的「行号 → 真实对象」映射，最稳。
        """
        return self._row_to_fav.get(self.list_widget.currentRow())

    def _remove_item(self) -> None:
        """删除选中的项目"""
        fav = self._current_fav()
        if fav is None:
            return
        if fav.get("cloud") and self._cloud_ready() and fav.get("id"):
            try:
                self._cloud_api.delete_fav_item(fav["id"])
            except Exception as exc:  # noqa: BLE001
                lprint(f"[收藏云同步] 删除云端失败: {exc}")
        idx = self._find_fav_index(fav)
        if idx >= 0:
            del self.favorites[idx]
            self._save_favorites()
            self._refresh_list()

    def _find_fav_index(self, fav: dict) -> int:
        """按对象身份在 self.favorites 中定位条目下标（返回 -1 表示不存在）。"""
        for i, f in enumerate(self.favorites):
            if f is fav:
                return i
        return -1

    def _execute_item(self) -> None:
        """执行选中的项目"""
        fav = self._current_fav()
        if fav is not None:
            FavoriteEntry.from_dict(fav).open(self)

    def _navigate_to_folder(self, folder_path: str) -> None:
        """导航到指定文件夹：优先 COM 直接驱动 Explorer，失败再回退按键模拟。"""
        # 1. 优先用 Shell COM 直接导航（不需抢占前台，最稳定）
        if self._navigate_explorer_com(folder_path):
            lprint(f" COM 已导航到 Explorer: {folder_path}")
            return

        user32 = ctypes.windll.user32

        # 优先使用快捷键触发时记录的前台窗口句柄
        if self._explorer_hwnd and user32.IsWindow(self._explorer_hwnd):
            hwnd = self._explorer_hwnd
            if self._navigate_explorer_hwnd(hwnd, folder_path):
                lprint(f" 已导航到缓存窗口（句柄 {hwnd}）: {folder_path}")
                return
            lprint(f" 缓存窗口导航失败，尝试其他 Explorer")

        # 尝试查找任意一个 Explorer 窗口
        found_hwnd = self._find_any_explorer_hwnd()
        if found_hwnd:
            if self._navigate_explorer_hwnd(found_hwnd, folder_path):
                self._explorer_hwnd = found_hwnd
                lprint(f" 已导航到 Explorer 窗口（句柄 {found_hwnd}）: {folder_path}")
                return

        # 没有 Explorer 窗口，打开新窗口
        lprint(f" 没有 Explorer 窗口，打开新窗口: {folder_path}")
        os.startfile(folder_path)

    def _navigate_explorer_com(self, folder_path: str) -> bool:
        """通过 Shell.Application COM 让已存在的 Explorer 窗口导航到目标路径。
        优先复用缓存的调用者窗口句柄，否则使用第一个文件资源管理器窗口。"""
        try:
            import pythoncom
            import win32com.client
        except Exception as e:
            lprint(f"COM 导航组件不可用: {e}")
            return False

        target_path = os.path.normpath(folder_path)
        if not os.path.isdir(target_path):
            return False

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            cached = int(self._explorer_hwnd) if self._explorer_hwnd else 0
            target = None
            fallback = None
            for window in shell.Windows():
                try:
                    whwnd = int(window.HWND)
                    _ = window.Document.Folder  # 仅文件资源管理器窗口可访问
                except Exception:
                    continue
                if cached and whwnd == cached:
                    target = window
                    break
                if fallback is None:
                    fallback = window
            target = target or fallback
            if target is None:
                return False
            try:
                target.Navigate2(target_path)
            except Exception:
                target.Navigate(target_path)
            try:
                ctypes.windll.user32.SetForegroundWindow(int(target.HWND))
            except Exception:
                pass
            self._explorer_hwnd = int(target.HWND)
            return True
        except Exception as e:
            lprint(f"COM 导航失败: {e}")
            return False
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    @classmethod
    def _find_any_explorer_hwnd(cls) -> int | None:
        """通过 FindWindowW 查找 CabinetWClass 窗口"""
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW("CabinetWClass", None)
        return hwnd if hwnd else None

    def _navigate_explorer_hwnd(self, hwnd: int, folder_path: str) -> bool:
        """通过模拟按键在 Explorer 窗口地址栏中导航"""
        user32 = ctypes.windll.user32
        VK_F4 = 0x73
        VK_RETURN = 0x0D
        VK_ESCAPE = 0x1B

        try:
            # 1. 让 Explorer 窗口置前
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)

            # 2. 发送 F4 激活地址栏（Explorer 标准快捷键）
            user32.keybd_event(VK_F4, 0, 0, 0)
            user32.keybd_event(VK_F4, 0, 2, 0)  # KEYEVENTF_KEYUP = 2
            time.sleep(0.15)

            # 3. 找到地址栏的 Edit 控件（F4 激活后地址栏变为可编辑的 Edit）
            edit_hwnd = self._find_address_bar_edit(hwnd)
            if edit_hwnd:
                # 直接设置文本 + 回车
                user32.SetWindowTextW(edit_hwnd, folder_path)
                user32.keybd_event(VK_RETURN, 0, 0, 0)
                user32.keybd_event(VK_RETURN, 0, 2, 0)
                lprint(f"  通过 Edit 控件导航: edit_hwnd={edit_hwnd}")
                return True

            # 4. 找不到 Edit 控件（Windows 11 XAML 地址栏），回退到剪贴板 + 粘贴方式
            lprint(f"  未找到 Edit 控件，回退到剪贴板粘贴方式")
            clipboard = QtWidgets.QApplication.clipboard()
            clipboard.setText(folder_path)
            time.sleep(0.05)
            # Ctrl+A 全选地址栏现有内容（防止路径被追加到末尾）
            user32.keybd_event(0x11, 0, 0, 0)  # VK_CONTROL down
            user32.keybd_event(0x41, 0, 0, 0)  # VK_A down
            user32.keybd_event(0x41, 0, 2, 0)  # VK_A up
            user32.keybd_event(0x11, 0, 2, 0)  # VK_CONTROL up
            time.sleep(0.05)
            # Ctrl+V 粘贴（替换已全选的内容）
            user32.keybd_event(0x11, 0, 0, 0)  # VK_CONTROL down
            user32.keybd_event(0x56, 0, 0, 0)  # VK_V down
            user32.keybd_event(0x56, 0, 2, 0)  # VK_V up
            user32.keybd_event(0x11, 0, 2, 0)  # VK_CONTROL up
            time.sleep(0.05)
            # 回车确认导航
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, 2, 0)
            return True

        except Exception as e:
            lprint(f" 导航失败: {e}")
            return False

    # ===== Explorer 窗口控件分析 =====

    @staticmethod
    def dump_explorer_tree() -> str:
        """遍历所有 Explorer 窗口的完整控件树，返回可读的层级文本"""
        user32 = ctypes.windll.user32
        find_ex = user32.FindWindowExW
        find_ex.restype = ctypes.c_void_p

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        results: list[str] = []

        def _enum_cb(hwnd, _lparam):
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            if cls_buf.value == "CabinetWClass":
                title_buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title_buf, 512)
                results.append(f"\n=== Explorer  hwnd={hwnd}  title={title_buf.value!r} ===")
                FolderFavoritesWidget._dump_children(hwnd, find_ex, user32, results, depth=1)
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return "\n".join(results) if results else "未找到任何 Explorer 窗口"

    @staticmethod
    def _dump_children(parent_hwnd: int, find_ex, user32, results: list[str], depth: int, max_depth: int = 15) -> None:
        """递归遍历子窗口并 append 到 results"""
        if depth > max_depth:
            return
        indent = "  " * depth
        child = find_ex(parent_hwnd, 0, None, None)
        while child:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, cls_buf, 256)
            cls_name = cls_buf.value

            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(child, title_buf, 256)
            title = title_buf.value

            # 获取窗口尺寸
            r = (ctypes.c_long * 4)()
            user32.GetWindowRect(child, ctypes.byref(r))
            w = r[2] - r[0]
            h = r[3] - r[1]

            info = f"{indent}[{cls_name}]  hwnd={child}  size={w}x{h}"
            if title:
                info += f"  text={title!r}"
            results.append(info)

            FolderFavoritesWidget._dump_children(child, find_ex, user32, results, depth + 1, max_depth)
            child = find_ex(parent_hwnd, child, None, None)

    @staticmethod
    def _find_address_bar_edit(explorer_hwnd: int) -> int | None:
        """查找 Explorer 地址栏的 Edit 控件（通过 FindWindowExW 逐层遍历）"""
        user32 = ctypes.windll.user32
        find = user32.FindWindowExW
        find.restype = ctypes.c_void_p

        # Explorer 控件层级:
        # CabinetWClass
        #   └─ WorkerW
        #       └─ ReBarWindow32
        #           └─ Address Band Root
        #               └─ ComboBoxEx32
        #                   └─ ComboBox
        #                       └─ Edit
        worker = find(explorer_hwnd, 0, "WorkerW", None)
        if not worker:
            return None
        rebar = find(worker, 0, "ReBarWindow32", None)
        if not rebar:
            return None
        # 遍历 ReBarWindow32 的子窗口查找 "Address Band Root"
        child = find(rebar, 0, None, None)
        addr_band = 0
        while child:
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, cls_buf, 256)
            if cls_buf.value == "Address Band Root":
                addr_band = child
                break
            child = find(rebar, child, None, None)
        if not addr_band:
            return None
        combo_ex = find(addr_band, 0, "ComboBoxEx32", None)
        if not combo_ex:
            return None
        combo = find(combo_ex, 0, "ComboBox", None)
        if not combo:
            return None
        edit = find(combo, 0, "Edit", None)
        return edit if edit else None

    def _show_context_menu(self, position: QtCore.QPoint) -> None:
        """显示右键菜单"""
        item = self.list_widget.itemAt(position)
        if item:
            menu = QtWidgets.QMenu()

            # 右键默认不改变选中项，导致下游 currentItem() 取到的是“上一次左键选中的行”
            # 而非本次右键点击的行；这里把右键命中的行强制设为当前行，保证
            # 修改/复制/删除/执行等操作都作用于右键点击的那个条目。
            clicked_row = self.list_widget.row(item)
            self.list_widget.setCurrentRow(clicked_row)
            # 直接取渲染时记录的真实对象，避免列表被筛选（隐藏部分条目）后行号与
            # self.favorites 下标错位，也避免 item.data(UserRole) 的深拷贝副本
            # 导致改名/云切换写不回去。
            fav = self._row_to_fav.get(clicked_row)
            if isinstance(fav, dict):
                entry = FavoriteEntry.from_dict(fav)
                item_type = entry.item_type

                # 各类型的菜单文案（打开动作 / 复制主值动作 / 修改动作 / 复制名称动作）
                _LABELS = {
                    "folder": ("打开文件夹", "复制路径", "修改文件夹", "复制文件夹名称"),
                    "url": ("打开网址", "复制网址", "修改网址", "复制网址名称"),
                    "command": ("执行命令", None, "修改命令", "复制命令名称"),
                }
                open_lbl, copy_val_lbl, rename_lbl, copy_name_lbl = _LABELS.get(
                    item_type, _LABELS["folder"]
                )

                execute_action = QtGui.QAction(f" {open_lbl}", self)

                # 仅文件夹有“跳转到此文件夹”
                if item_type == "folder":
                    navigate_action = QtGui.QAction(" 跳转到此文件夹", self)
                    navigate_action.triggered.connect(
                        lambda checked=False, v=entry.expanded_value: self._navigate_to_folder(v)
                    )
                else:
                    navigate_action = None

                # 复制主值（路径/网址）；命令类型无此项
                if copy_val_lbl:
                    copy_path_action = QtGui.QAction(f" {copy_val_lbl}", self)
                    copy_path_action.triggered.connect(
                        lambda checked=False, v=entry.expanded_value: self._copy_path_to_clipboard(v)
                    )
                else:
                    copy_path_action = None

                rename_action = QtGui.QAction(f" {rename_lbl}", self)
                rename_action.triggered.connect(
                    lambda checked=False, f=fav: self._rename_item(f)
                )
                copy_name_action = QtGui.QAction(f" {copy_name_lbl}", self)
                copy_name_action.triggered.connect(
                    lambda checked=False, n=entry.expanded_name: self._copy_name_to_clipboard(n)
                )

                execute_action.triggered.connect(self._execute_item)
                menu.addAction(execute_action)
                if navigate_action:
                    menu.addAction(navigate_action)
                if copy_path_action:
                    menu.addAction(copy_path_action)
                menu.addAction(rename_action)
                menu.addAction(copy_name_action)

                # 云标记切换：本地 ↔ 云同步（右键）
                cloud_action = QtGui.QAction(
                    "☁ 转为本地" if fav.get("cloud") else "☁ 同步到云", self
                )
                cloud_action.triggered.connect(
                    lambda: self._toggle_cloud(fav)
                )
                menu.addAction(cloud_action)

            remove_action = QtGui.QAction(" 删除", self)
            remove_action.triggered.connect(self._remove_item)
            menu.addAction(remove_action)

            menu.addSeparator()
            copy_item_action = QtGui.QAction(" 复制条目", self)
            copy_item_action.triggered.connect(
                lambda checked=False, it=item: self._copy_item(it)
            )
            menu.addAction(copy_item_action)
            paste_item_action = QtGui.QAction(" 粘贴条目", self)
            paste_item_action.setEnabled(self._read_item_payload() is not None)
            paste_item_action.triggered.connect(self._paste_item)
            menu.addAction(paste_item_action)

            # 云同步：一键把所有本地项上传到数据库（仅登录后可用）
            if self._cloud_ready():
                menu.addSeparator()
                sync_all_action = QtGui.QAction("☁ 全部同步到数据库", self)
                sync_all_action.triggered.connect(self._sync_all_local_to_cloud)
                menu.addAction(sync_all_action)

            menu.exec(self.list_widget.mapToGlobal(position))
        else:
            # 空白处：仅当剪贴板有可粘贴条目时显示「粘贴条目」
            if self._read_item_payload() is not None:
                menu = QtWidgets.QMenu()
                paste_item_action = QtGui.QAction(" 粘贴条目", self)
                paste_item_action.triggered.connect(self._paste_item)
                menu.addAction(paste_item_action)
                menu.exec(self.list_widget.mapToGlobal(position))

    # ── 跨标签复制/粘贴（保留来源类型）────────────────────────────
    def _read_item_payload(self) -> dict | None:
        return _favorites_read_from_clipboard()

    def _copy_item(self, item: QtWidgets.QListWidgetItem) -> None:
        fav = item.data(QtCore.Qt.UserRole)
        if isinstance(fav, dict):
            _favorites_copy_to_clipboard(fav)

    def _paste_item(self) -> None:
        data = _favorites_read_from_clipboard()
        if data is None:
            return
        self.favorites.append(data)
        self._save_favorites()
        self._refresh_list()


    def _copy_path_to_clipboard(self, path: str) -> None:
        """将路径复制到剪贴板"""
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(path)
        lprint(f"已复制路径到剪贴板: {path}")

    def _copy_name_to_clipboard(self, name: str) -> None:
        """将名称复制到剪贴板"""
        clipboard = QtWidgets.QApplication.clipboard()
        clipboard.setText(name)
        lprint(f"已复制名称到剪贴板: {name}")

    def _rename_item(self, fav: dict) -> None:
        """重命名收藏夹项目（按条目 dict 定位，避免列表筛选导致行号错位）。"""
        if not isinstance(fav, dict):
            return
        entry = FavoriteEntry.from_dict(fav)
        item_type = entry.item_type

        # 准备对话框参数
        name = entry.name
        value = entry.value

        # 创建并显示对话框
        dialog = RenameItemDialog(self, item_type, name, value)

        if dialog.exec() == QtWidgets.QDialog.Accepted:
            new_name, new_value = dialog.get_result()

            if new_name:
                fav["name"] = new_name
            if new_value:
                entry.value = new_value  # 写回该类型对应的主值字段
        
        if fav.get("cloud") and self._cloud_ready() and fav.get("id"):
            try:
                self._cloud_api.update_fav_item(
                    fav["id"], **self._fav_cloud_payload(fav)
                )
            except Exception as exc:  # noqa: BLE001
                lprint(f"[收藏云同步] 更新云端失败: {exc}")
        
        self._save_favorites()
        self._refresh_list()

    def _add_command(self) -> None:
        """添加新命令"""
        # 创建自定义对话框，包含名称和命令两个输入框
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("添加命令")
        dialog.setModal(True)
        dialog.setMinimumWidth(400)
        
        layout = QtWidgets.QVBoxLayout(dialog)
        
        # 名称输入
        name_label = QtWidgets.QLabel("命令名称:")
        name_input = QtWidgets.QLineEdit()
        name_input.setPlaceholderText("请输入命令名称")
        layout.addWidget(name_label)
        layout.addWidget(name_input)
        
        # 命令内容输入
        command_label = QtWidgets.QLabel("命令内容:")
        command_input = QtWidgets.QLineEdit()
        command_input.setPlaceholderText("请输入要执行的命令，可用 {y}{m}{d} {pc} {user} 等变量")
        layout.addWidget(command_label)
        layout.addWidget(command_input)

        attach_var_preview(layout, [("名称", name_input), ("命令", command_input)])
        
        # 按钮
        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)
        
        # 显示对话框
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            name = name_input.text().strip()
            command = command_input.text().strip()
            
            if not name or not command:
                return
            
            # 添加到收藏夹
            self.favorites.append({
                "type": "command",
                "name": name,
                "command": command
            })
            self._save_favorites()
            self._refresh_list()

    # ===== 剪贴板历史相关方法 =====

    def _refresh_clipboard_display(self) -> None:
        """初始化时刷新剪贴板显示（QListView 由 model 驱动，无需手动建项）"""
        self._update_clipboard_count_label()
        # 初始化当前剪贴板内容
        clipboard = QtWidgets.QApplication.clipboard()
        self._last_clipboard_text = clipboard.text().strip()
        self._last_clipboard_key = ("", "")

    def _clipboard_key(self, item: dict) -> tuple:
        """剪贴板条目去重键（与模型一致）。"""
        return ClipboardHistoryModel._dedupe_key(item)

    def _snapshot_clipboard_item(self) -> dict | None:
        """读取系统剪贴板并生成一条历史条目。

        优先级：图片 > 文件 > 文本。无可记录内容返回 None。
        """
        clipboard = QtWidgets.QApplication.clipboard()
        mime = clipboard.mimeData()
        # 1) 图片（剪贴板可能图文并存，如从浏览器/Word 复制图文）
        try:
            if mime.hasImage():
                image = clipboard.image()
                if not image.isNull():
                    text = ""
                    try:
                        t = clipboard.text()
                        if t and t.strip():
                            text = t.strip()
                    except Exception:
                        pass
                    return self._build_image_item(image, text=text)
        except Exception as e:
            lprint(f"读取剪贴板图片失败: {e}")
        # 2) 本地文件 / 文件夹
        if mime.hasUrls():
            files = []
            for u in mime.urls():
                try:
                    local = u.toLocalFile()
                except Exception:
                    local = ""
                if local and os.path.exists(local):
                    files.append(os.path.normpath(local))
            if files:
                return self._build_file_item(files)
        # 3) 文本
        try:
            text = clipboard.text()
        except Exception as e:
            lprint(f"读取剪贴板文本失败: {e}")
            return None
        if text is not None:
            text = text.strip()
            if text:
                return {
                    "kind": "text",
                    "text": text,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
        return None

    def _build_image_item(self, image, text: str = "") -> dict | None:
        """图片条目：PNG 原图落盘到 clipboard_images/{md5}.png，历史只存引用。

        若剪贴板同时含文字（图文混排复制），一并保存到 text 字段供预览/搜索。
        """
        if image.isNull():
            return None
        # 超长边等比缩放，防止超大截图撑爆磁盘
        if max(image.width(), image.height()) > CLIPBOARD_IMAGE_MAX_SIDE:
            image = image.scaled(
                CLIPBOARD_IMAGE_MAX_SIDE, CLIPBOARD_IMAGE_MAX_SIDE,
                QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
            )
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        try:
            if not image.save(buf, "PNG"):
                return None
        finally:
            buf.close()
        data = bytes(ba)
        if not data:
            return None
        md5 = hashlib.md5(data).hexdigest()[:16]
        img_dir = self.favorites_dir / CLIPBOARD_IMAGES_DIR_NAME
        img_dir.mkdir(parents=True, exist_ok=True)
        img_path = img_dir / f"{md5}.png"
        if not img_path.exists():
            try:
                img_path.write_bytes(data)
            except OSError as e:
                lprint(f"保存剪贴板图片失败: {e}")
                return None
        # 顺带预生成 46px 小缩略图（{md5}_t.png），渲染缩略图时避免读 4096px 大图
        self._write_thumb_file(img_path, image)
        # 图文并存时保存真实文本（截断防历史 JSON 过大），否则用占位符
        display_text = "[图片]"
        if text and text.strip():
            display_text = text.strip()
            if len(display_text) > 500:
                display_text = display_text[:500] + "…"
        return {
            "kind": "image",
            "text": display_text,
            "md5": md5,
            "image_path": str(img_path),
            "width": image.width(),
            "height": image.height(),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _write_thumb_file(img_path, image, size: int = 46) -> None:
        """生成小缩略图文件（{stem}_t.png），供历史面板/弹窗渲染用。失败静默。"""
        try:
            thumb_path = f"{os.path.splitext(str(img_path))[0]}_t.png"
            if os.path.exists(thumb_path):
                return
            if max(image.width(), image.height()) > size:
                image = image.scaled(
                    size, size,
                    QtCore.Qt.KeepAspectRatio,
                    QtCore.Qt.SmoothTransformation,
                )
            image.save(thumb_path, "PNG")
        except Exception:
            pass

    def _build_file_item(self, files: list) -> dict:
        """文件条目：保存本地文件/文件夹路径列表。"""
        files = [os.path.normpath(f) for f in files]
        if len(files) == 1:
            display = os.path.basename(files[0]) or files[0]
        else:
            display = f"{len(files)} 个文件"
        return {
            "kind": "file",
            "text": display,
            "files": list(files),
            "count": len(files),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _on_clipboard_changed(self) -> None:
        """剪贴板内容变化时的回调（Qt 信号驱动）：记录文本 / 图片 / 文件。"""
        if not hasattr(self, "clipboard_model"):
            return
        try:
            item = self._snapshot_clipboard_item()
        except Exception as e:
            lprint(f"读取剪贴板失败: {e}")
            self._last_clipboard_key = ("", "")
            return
        if item is None:
            self._last_clipboard_key = ("", "")
            return
        key = self._clipboard_key(item)
        # 与上次相同（防抖）或已在历史中 → 跳过
        if key == self._last_clipboard_key or self.clipboard_model.contains_key(key):
            self._last_clipboard_key = key
            return
        self.clipboard_model.prepend(item, CLIPBOARD_MAX_STORED)
        self._schedule_save_clipboard()
        self._update_clipboard_count_label()
        self._last_clipboard_key = key

    def _on_clipboard_search_changed(self, text: str) -> None:
        """搜索框内容变化：交给代理模型过滤（虚拟化渲染，无需手动建项）。"""
        self._clipboard_proxy.setFilterFixedString(text.strip())
        self._update_clipboard_count_label()

    def _update_clipboard_count_label(self) -> None:
        """更新记录数量显示。"""
        if not hasattr(self, "clipboard_count_label"):
            return
        total = self.clipboard_model.rowCount()
        matched = self._clipboard_proxy.rowCount()
        if matched != total:
            self.clipboard_count_label.setText(f"匹配 {matched}/{total} 条")
        else:
            self.clipboard_count_label.setText(f"{total} 条")

    def _dedupe_clipboard_history(self) -> None:
        """清理重复：相同文本只保留最新一条（保持时间倒序）。"""
        removed = self.clipboard_model.dedupe()
        if removed <= 0:
            QtWidgets.QMessageBox.information(self, "清理重复", "没有发现重复记录。")
            return
        self._schedule_save_clipboard()
        self._update_clipboard_count_label()
        lprint(f" 已清理 {removed} 条重复剪贴板记录")

    def _clipboard_item_at(self, point: QtCore.QPoint) -> dict | None:
        """根据视图坐标取对应行的条目 dict。"""
        index = self.clipboard_list.indexAt(point)
        if not index.isValid():
            return None
        return index.data(ClipboardHistoryModel.ItemRole)

    def _show_clipboard_context_menu(self, position: QtCore.QPoint) -> None:
        """显示剪贴板历史右键菜单（按 kind 提供不同操作）"""
        item = self._clipboard_item_at(position)
        if not item:
            return
        kind = item.get("kind", "text")
        clipboard = QtWidgets.QApplication.clipboard()
        menu = QtWidgets.QMenu()

        if kind == "text":
            act = menu.addAction(" 复制文本")
            act.triggered.connect(
                lambda: clipboard.setText(str(item.get("text", "") or ""))
            )
        elif kind == "image":
            img_path = item.get("image_path", "")
            act_copy = menu.addAction(" 复制图片")
            act_copy.triggered.connect(
                lambda: self._copy_image_to_clipboard(img_path))
            act_save = menu.addAction(" 另存图片为...")
            act_save.triggered.connect(
                lambda: self._save_image_to_file(img_path))
        elif kind == "file":
            files = list(item.get("files", []) or [])
            act_copy = menu.addAction(" 复制文件")
            act_copy.triggered.connect(
                lambda: self._copy_files_to_clipboard(files))
            act_open = menu.addAction(" 打开所在文件夹")
            act_open.triggered.connect(
                lambda: self._open_files_location(files))

        menu.addSeparator()
        delete_action = menu.addAction(" 删除此条")
        delete_action.triggered.connect(
            lambda: self._delete_clipboard_item(item))
        menu.addSeparator()
        clear_action = menu.addAction(" 清除全部历史")
        clear_action.triggered.connect(self._clear_clipboard_history)
        menu.exec(self.clipboard_list.viewport().mapToGlobal(position))

    def _copy_image_to_clipboard(self, img_path: str) -> None:
        """把历史图片重新放回系统剪贴板。"""
        if not img_path or not os.path.exists(img_path):
            return
        try:
            QtWidgets.QApplication.clipboard().setImage(QtGui.QImage(img_path))
        except Exception as e:
            lprint(f"复制图片失败: {e}")

    def _save_image_to_file(self, img_path: str) -> None:
        """另存历史图片到用户指定位置。"""
        if not img_path or not os.path.exists(img_path):
            QtWidgets.QMessageBox.warning(self, "图片不存在", "该图片文件已不存在。")
            return
        default_name = os.path.basename(img_path) or "clipboard_image.png"
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存图片为", default_name, "PNG 图片 (*.png);;所有文件 (*.*)")
        if not target:
            return
        try:
            image = QtGui.QImage(img_path)
            if not image.save(target, "PNG"):
                QtWidgets.QMessageBox.warning(self, "保存失败", "图片保存失败。")
        except Exception as e:
            lprint(f"另存图片失败: {e}")

    def _copy_files_to_clipboard(self, files: list) -> None:
        """把历史文件重新放回系统剪贴板（用于粘贴到资源管理器等）。"""
        if not files:
            return
        urls = [QtCore.QUrl.fromLocalFile(f) for f in files]
        mime = QtCore.QMimeData()
        mime.setUrls(urls)
        QtWidgets.QApplication.clipboard().setMimeData(mime)

    def _open_files_location(self, files: list) -> None:
        """在资源管理器中定位文件/文件夹（Windows）。"""
        if sys.platform != "win32" or not files:
            return
        try:
            if len(files) == 1:
                p = files[0]
                if os.path.isdir(p):
                    subprocess.Popen(["explorer", p])
                else:
                    subprocess.Popen(["explorer", "/select,", p])
            else:
                subprocess.Popen(["explorer", os.path.dirname(files[0])])
        except Exception as e:
            lprint(f"打开所在文件夹失败: {e}")

    def _apply_clipboard_item_to_system(self, item: dict) -> None:
        """把一条剪贴板条目按类型恢复到系统剪贴板（text/image/file）。"""
        if not item:
            return
        kind = item.get("kind", "text")
        clipboard = QtWidgets.QApplication.clipboard()
        if kind == "image":
            img_path = item.get("image_path", "")
            if not img_path or not os.path.exists(img_path):
                return
            clipboard.setImage(QtGui.QImage(img_path))
        elif kind == "file":
            files = list(item.get("files", []) or [])
            if not files:
                return
            urls = [QtCore.QUrl.fromLocalFile(f) for f in files]
            mime = QtCore.QMimeData()
            mime.setUrls(urls)
            clipboard.setMimeData(mime)
        else:
            text = str(item.get("text", "") or "")
            if not text:
                return
            clipboard.setText(text)

    def _use_clipboard_item(self, index: QtCore.QModelIndex) -> None:
        """双击使用剪贴板项：恢复对应类型到系统剪贴板，填入调用窗口并隐藏本窗口。"""
        item = index.data(ClipboardHistoryModel.ItemRole) if index.isValid() else None
        if not item:
            return
        kind = item.get("kind", "text")
        self._apply_clipboard_item_to_system(item)
        display = str(item.get("text", "") or "")[:50]
        lprint(f"已使用剪贴板项: {display or kind}")
        self._paste_clipboard_to_caller()

    def _prewarm_clipboard_popup(self) -> None:
        """启动后空闲期预热剪贴板弹窗（离屏显示一次）。

        大历史（2000 条）下，弹窗首次真实显示要做一次全量行布局（非统一行高的
        QListView 需逐行算高），不预热的话首次 Win+V 会有明显延迟。
        WA_DontShowOnScreen 让 show() 只触发布局、不上屏。
        """
        try:
            if getattr(self, "_clipboard_popup", None) is not None:
                return
            if not self.clipboard_model.items():
                return  # 无历史数据，无需预热
            popup = ClipboardHistoryPopup(self)
            self._clipboard_popup = popup
            popup._fit_height_to_content()
            popup.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            popup.show()
            QtCore.QCoreApplication.processEvents()
            popup.hide()
            popup.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            lprint("剪贴板弹窗已预热（离屏布局）")
        except Exception as e:
            lprint(f"剪贴板弹窗预热失败: {e}")

    def popup_clipboard_history(self, caller_hwnd: int = 0) -> None:
        """Win+V 触发：弹出「仅剪贴板历史」小窗口（置顶、不抢调用者焦点）。"""
        popup = getattr(self, "_clipboard_popup", None)
        if popup is None:
            popup = ClipboardHistoryPopup(self)
            self._clipboard_popup = popup
        popup.show_popup(caller_hwnd)

    def _paste_clipboard_to_caller(self) -> bool:
        hwnd = int(self._explorer_hwnd) if self._explorer_hwnd else 0
        if not hwnd or sys.platform != "win32":
            return False
        try:
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            top = self.window()
            top.hide()
            QtWidgets.QApplication.processEvents()
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            QtCore.QTimer.singleShot(80, self._send_paste_shortcut)
            return True
        except Exception as e:
            lprint(f"粘贴到调用窗口失败: {e}")
            return False

    def _send_paste_shortcut(self) -> None:
        try:
            user32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_V = 0x56
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            user32.keybd_event(VK_V, 0, 0, 0)
            user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception as e:
            lprint(f"发送粘贴快捷键失败: {e}")

    def _delete_clipboard_item(self, item: dict) -> None:
        """删除单条剪贴板记录（同时清理不再被引用的图片文件）"""
        removed = self.clipboard_model.remove_key(self._clipboard_key(item))
        if removed is not None:
            self._cleanup_orphan_image_files()
            self._schedule_save_clipboard()
            self._update_clipboard_count_label()

    def _cleanup_orphan_image_files(self) -> None:
        """删除不再被任何历史条目引用的图片缓存文件。"""
        referenced = {
            str(it.get("image_path", ""))
            for it in self.clipboard_model.items()
            if it.get("kind") == "image" and it.get("image_path")
        }
        img_dir = self.favorites_dir / CLIPBOARD_IMAGES_DIR_NAME
        if not img_dir.is_dir():
            return
        try:
            for f in img_dir.iterdir():
                if f.is_file() and str(f) not in referenced:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def _clear_clipboard_image_dir(self) -> None:
        """删除全部图片缓存文件（清除历史时调用）。"""
        img_dir = self.favorites_dir / CLIPBOARD_IMAGES_DIR_NAME
        if not img_dir.is_dir():
            return
        try:
            for f in img_dir.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def _clear_clipboard_history(self) -> None:
        """清除剪贴板历史（连同图片缓存文件一起删除）"""
        if self.clipboard_model.rowCount() == 0:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "确认清除",
            f"确定要清除全部 {self.clipboard_model.rowCount()} 条剪贴板历史记录吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self.clipboard_model.clear()
            self._clear_clipboard_image_dir()
            self._schedule_save_clipboard()
            self._update_clipboard_count_label()
            lprint(" 剪贴板历史已清除")

    def _flush_clipboard_save(self) -> None:
        """若有挂起的防抖写盘请求，立即落盘（用于隐藏/关闭前）。"""
        if self._clipboard_save_timer.isActive():
            self._clipboard_save_timer.stop()
            self._save_clipboard_history()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        self._flush_clipboard_save()
        # 主面板隐藏时一并收起悬停预览浮层
        prev = getattr(self, "_clipboard_hover_preview", None)
        if prev is not None:
            prev._hide()
        super().hideEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._flush_clipboard_save()
        super().closeEvent(event)
