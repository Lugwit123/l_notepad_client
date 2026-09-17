# -*- coding: utf-8 -*-

from __future__ import annotations

import subprocess
import sys
import json
import os
import re
import ctypes
import logging
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime

from pathlib import Path, PurePosixPath

from PySide6 import QtCore, QtGui, QtUiTools, QtWidgets
try:
    import shiboken6
except Exception:  # pragma: no cover - shiboken6 随 PySide6 提供，兜底避免导入异常
    shiboken6 = None

from .api_client import ApiError, LogDto, NotepadApi, NoteDto
from . import clipboard_recorder
from . import history_store
from . import paths
from . import server_config
from .folder_favorites_widget import FolderFavoritesWidget as FolderFavoritesPanel
from .account_favorites_widget import AccountFavoritesWidget as AccountFavoritesPanel
from .settings_widget import SettingsWidget
from .version_diff_dialog import VersionDiffDialog
from .file_store import sanitize_title_to_filename
from pytracemp import lprint

# 从 l_qt_wgt_lib 导入代码编辑器组件
from l_qt_wgt_lib.smart_widget import (
    CodeEditorWidget,
    apply_indent_display_options,
    load_indent_display_options_from_settings,
)
from l_qt_wgt_lib.tray_window import TrayAwareMixin


SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_MODELS_URL = "https://api.siliconflow.cn/v1/models"
SILICONFLOW_PRICING_URL = "https://www.siliconflow.com/pricing"
# 国内站价格页（下载快且稳定，作为首选数据源；.com 站仅作兜底）
SILICONFLOW_PRICING_CN_URL = "https://www.siliconflow.cn/pricing"
DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-7B-Instruct"
SILICONFLOW_MODELS = [
    "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-72B-Instruct",
    "deepseek-ai/DeepSeek-V4-Flash",
    "Pro/zai-org/GLM-4.7",
]
SILICONFLOW_API_KEY = os.environ.get(
    "SILICONFLOW_API_KEY",
    "sk-gzwtmzfhglvibdbvrttmsuuqsyyjxghxlxzdhubdefmshqoi",
)

ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
ZHIPU_MODELS_URL = "https://open.bigmodel.cn/api/paas/v4/models"
DEFAULT_ZHIPU_MODEL = "glm-4-flash"
ZHIPU_MODELS = [
    "glm-4-flash",
    "glm-4-air",
    "glm-4-plus",
    "glm-4-long",
]
DEFAULT_ZHIPU_KEY = "263c58d09135c4f088b0d436e3b89bfb.hXFGig2ucu4xe5PT"
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", DEFAULT_ZHIPU_KEY)

DEFAULT_MODEL_PRESETS = SILICONFLOW_MODELS

# AI 提供商配置：{provider_name: (url, api_key, models, default_model)}
AI_PROVIDERS: dict[str, tuple[str, str, list[str], str]] = {
    "SiliconFlow": (SILICONFLOW_URL, SILICONFLOW_API_KEY, SILICONFLOW_MODELS, DEFAULT_SILICONFLOW_MODEL),
    "智谱 Zhipu": (ZHIPU_URL, ZHIPU_API_KEY, ZHIPU_MODELS, DEFAULT_ZHIPU_MODEL),
}
DEFAULT_PROVIDER = "SiliconFlow"
ASK_AI_ITEM_ID = "__ask_ai__"
ASK_AI_SESSION_PREFIX = "__ask_ai_session__:"
NOTE_REORDER_MIME = "application/x-l-notepad-note-reorder"
EXTERNAL_FILE_PREFIX = "__external_file__:"
IPC_FILE_PREFIX = "__ipc_file__:"
IPC_FILE_FOLDER_ID = "__ipc_file_folder__"
SERVER_LOG_PREFIX = "__server_log__:"
SERVER_LOG_FOLDER_ID = "__server_log_folder__"
SERVER_LOG_SUB_PREFIX = "__server_log_sub__:"
# 全文检索命中的「知识库/未映射笔记」行：UserRole 用字符串前缀 + hits 下标，
# 绝不能是 int（否则会被当成笔记 id 打开/重命名，见 _on_selection_changed_inner）
SEARCH_HIT_PREFIX = "__searchhit__:"
# 搜索框去抖时长（毫秒）
_SEARCH_DEBOUNCE_MS = 350
EXTERNAL_FILES_STATE_NAME = "external_files.json"
# 本地文件外部修改轮询间隔（毫秒）：QFileSystemWatcher 丢目标时的兜底
_LOCAL_FILE_POLL_MS = 1500


def _local_files_from_mime(mime) -> list[str]:
    """从拖放数据中取出本地文件路径（资源管理器拖入的 uri-list）。"""
    if mime is None:
        return []
    try:
        if not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            raw = url.toLocalFile()
            if raw:
                paths.append(str(Path(raw)))
        return paths
    except Exception:
        return []
# 收藏笔记在笔记树/列表中使用的全局置顶分组名
FAVORITES_GROUP_NAME = "★ 收藏"
LOG_VIEW_CONTENT_CACHE_KEY = "__l_notepad_log_view__"
LOG_LEVEL = logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger().setLevel(LOG_LEVEL)
logger = logging.getLogger(__name__)
_SILICONFLOW_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class ModelPrice:
    input_per_m: float
    output_per_m: float
    currency: str = "$"


def _load_stylesheet() -> str:
    """从文件加载样式表"""
    style_path = Path(__file__).resolve().parent / "style.qss"
    if style_path.exists():
        return style_path.read_text(encoding="utf-8")
    return ""


_AI_PROMPT_PLACEHOLDER = (
    '在这里输入问题，然后点击"问AI"。\n\n'
    "支持多会话与上下文记忆。\n"
    "支持模型：硅基流动 SiliconFlow / 智谱 Zhipu。"
)
_AI_ANSWER_PLACEHOLDER = "AI 回答会显示在这里（支持多会话上下文）"

# 版本行三个下拉框共用（字号偏小，避免撑高整行）
_VERSION_COMBO_QSS = (
    "QComboBox { font-size: 10px; padding: 1px 4px; }"
    "QComboBox QAbstractItemView { font-size: 10px; }"
    "QComboBox QAbstractItemView::item { padding: 1px 4px; min-height: 16px; }"
)


def _is_ai_prompt_placeholder_body(text: str) -> bool:
    """历史版本曾把提示文案写入文档，与 placeholder 等价时视为空。"""
    return text.strip() == _AI_PROMPT_PLACEHOLDER.strip()


def _set_code_editor_document(editor: CodeEditorWidget, text: str) -> None:
    """写入正文；空字符串时清空文档以显示 placeholder（勿把提示文案 setPlainText）。"""
    if text:
        editor.setPlainText(text)
    else:
        editor.clear()


def _plain_text_line_wrap_mode(mode) -> QtWidgets.QPlainTextEdit.LineWrapMode:
    """QTextEdit.LineWrapMode 与 QPlainTextEdit.LineWrapMode 在 PySide6 中类型不兼容。"""
    if isinstance(mode, QtWidgets.QPlainTextEdit.LineWrapMode):
        return mode
    plain = QtWidgets.QPlainTextEdit.LineWrapMode
    text = QtWidgets.QTextEdit.LineWrapMode
    if mode == text.NoWrap:
        return plain.NoWrap
    if mode in (text.WidgetWidth, text.FixedPixelWidth, text.FixedColumnWidth):
        return plain.WidgetWidth
    return plain(int(mode))


def _apply_text_edit_appearance(
    editor: CodeEditorWidget,
    source: QtWidgets.QTextEdit | CodeEditorWidget,
) -> None:
    if isinstance(source, CodeEditorWidget):
        placeholder = source.placeholderText()
        wrap_mode = source.lineWrapMode()
        read_only = source.isReadOnly()
        accept_drops = source.editor().acceptDrops()
        size_policy = source.sizePolicy()
    else:
        placeholder = source.placeholderText()
        wrap_mode = source.lineWrapMode()
        read_only = source.isReadOnly()
        accept_drops = source.acceptDrops()
        size_policy = source.sizePolicy()
    editor.clear()
    editor.setLineWrapMode(_plain_text_line_wrap_mode(wrap_mode))
    editor.setReadOnly(read_only)
    if accept_drops:
        editor.setAcceptDrops(True)
    # 正文为空；placeholder 仅作灰色提示，不写入文档
    editor.setPlaceholderText(placeholder)
    # 继承尺寸策略，确保撑满父布局
    editor.setSizePolicy(size_policy)


def replace_text_edit_with_code_editor(
    old_widget: QtWidgets.QTextEdit,
    obj_name: str,
) -> CodeEditorWidget:
    """将 .ui 中的 QTextEdit 替换为 CodeEditorWidget（保留布局与外观属性）。"""
    parent = old_widget.parentWidget()
    layout = parent.layout() if parent is not None else None
    new_editor = CodeEditorWidget(parent)
    new_editor.setObjectName(obj_name)
    _apply_text_edit_appearance(new_editor, old_widget)
    # 搜索栏向上偏移一个自身高度，避免遮挡内容
    new_editor.set_search_bar_y_offset(-new_editor.search_bar_height())
    
    if layout is not None:
        # 查找旧 widget 在布局中的索引和属性
        index = -1
        stretch = 0
        alignment = 0
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget() is old_widget:
                index = i
                stretch = layout.stretch(i)
                alignment = layout.alignment() if hasattr(layout, 'alignment') else 0
                break
        
        # 移除旧 widget 并插入新 widget
        if index >= 0:
            layout.removeWidget(old_widget)
            # 根据布局类型插入到正确位置
            if isinstance(layout, QtWidgets.QVBoxLayout) or isinstance(layout, QtWidgets.QHBoxLayout):
                layout.insertWidget(index, new_editor, stretch)
            else:
                layout.addWidget(new_editor)
        else:
            layout.replaceWidget(old_widget, new_editor)
    
    old_widget.deleteLater()
    return new_editor


def create_code_editor_widget(
    parent: QtWidgets.QWidget,
    obj_name: str,
    template: QtWidgets.QTextEdit | CodeEditorWidget | None = None,
) -> CodeEditorWidget:
    """新建 CodeEditorWidget，可选从模板（QTextEdit 或已替换的 CodeEditorWidget）复制外观。"""
    editor = CodeEditorWidget(parent)
    editor.setObjectName(obj_name)
    editor.clear()
    if template is not None:
        _apply_text_edit_appearance(editor, template)
    # 搜索栏向上偏移一个自身高度，避免遮挡内容
    editor.set_search_bar_y_offset(-editor.search_bar_height())
    return editor


@dataclass
class UiState:
    current_note_id: int | None = None
    dirty: bool = False


@dataclass
class AiSession:
    session_id: str
    title: str
    messages: list[dict[str, str]]
    draft_prompt: str = ""
    streaming_text: str = ""
    reasoning_text: str = ""   # 推理阶段思考内容（DeepSeek-R1 等）
    in_flight: bool = False


class AiBridge(QtCore.QObject):
    chunk = QtCore.Signal(int, str, str)
    reasoning_chunk = QtCore.Signal(int, str, str)   # 推理阶段 reasoning_content
    finished = QtCore.Signal(int, str, bool, str, str)


class ModelBridge(QtCore.QObject):
    finished = QtCore.Signal(bool, object)


class PriceBridge(QtCore.QObject):
    finished = QtCore.Signal(bool, object)


class _NotesLoaderThread(QtCore.QThread):
    """后台加载笔记列表（本地为目录扫描，远程为 HTTP 请求）。

    启动期间在 QThread 中执行 api.list_notes()，完成后经 loaded 信号回到
    主线程刷新 UI，避免文件多/网络慢时卡住界面首帧。
    """

    loaded = QtCore.Signal(object, object)  # (notes, error)

    def __init__(self, api, parent=None) -> None:
        super().__init__(parent)
        self._api = api

    def run(self) -> None:
        try:
            notes = self._api.list_notes()
        except Exception as exc:  # noqa: BLE001
            self.loaded.emit(None, exc)
        else:
            self.loaded.emit(notes, None)


class _SearchThread(QtCore.QThread):
    """后台调用后端全文检索接口（模糊 + 倒排索引）。

    与 _NotesLoaderThread 同构：在 QThread 中执行 api.search()，完成后经 loaded
    信号回到主线程刷新 UI，避免搜索时阻塞界面。
    """

    loaded = QtCore.Signal(object, object)  # (result, error)

    def __init__(self, api, query: str, *, limit: int = 200, parent=None) -> None:
        super().__init__(parent)
        self._api = api
        self._query = query
        self._limit = limit

    def run(self) -> None:
        try:
            result = self._api.search(self._query, limit=self._limit)
        except Exception as exc:  # noqa: BLE001
            self.loaded.emit(None, exc)
        else:
            self.loaded.emit(result, None)


# ---- 左侧文件树 delegate：将 "文件名\n日期" 分别绘制成两行不同颜色 ----

class _NoteTreeItemDelegate(QtWidgets.QStyledItemDelegate):
    """将 item 文本按换行符拆分成两行：第一行文件名粗体，第二行日期橙色。

    同时（参考 l_pyside6_uv 的 StarDelegate）在笔记项右侧绘制收藏星标：
    - 已收藏始终显示实心 ★；
    - 未收藏仅在鼠标悬停该行时显示空心 ☆；
    - 鼠标移到右侧星标热区时，星标在 ☆/★ 间切换作为可点击反馈。
    """

    _COLOR_MAIN = QtGui.QColor("#E9EEF5")
    _COLOR_DATE = QtGui.QColor("#FFA657")
    _COLOR_FOLDER = QtGui.QColor("#89DDFF")
    _COLOR_ASK_AI = QtGui.QColor("#B794F4")
    _COLOR_STAR = QtGui.QColor("#FFB800")
    # 硬盘上已删除的文件条目（✕ 标记）用告警色
    _COLOR_MISSING = QtGui.QColor("#FF6B6B")
    # 左侧填充
    _PADDING_LEFT = 2
    # 星标可点击热区宽度（从右侧边缘往左）
    STAR_HOTZONE = 26
    # 父级目录高亮叠加色（alpha 0.8）
    _COLOR_PARENT_HIGHLIGHT = QtGui.QColor(77, 130, 220, 24)
    # 标记 item 为“父级高亮”的自定义 role
    _PARENT_HIGHLIGHT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 99

    def __init__(self, parent=None, owner=None) -> None:
        super().__init__(parent)
        self._owner = owner

    def _star_state(self, option, index) -> tuple[bool, str | None]:
        """返回 (是否已收藏, 需要绘制的星标字符)；None 表示无需绘制。"""
        key = self._favorite_key_for_role(
            index.data(QtCore.Qt.ItemDataRole.UserRole)
        )
        if key is None:
            return False, None
        owner = self._owner
        fav = bool(owner is not None and owner._is_favorite(key))
        view = option.widget
        hovered = bool(view is not None and getattr(view, "_fav_hover_key", None) == key)
        if not fav and not hovered:
            return fav, None
        star_over = hovered and bool(getattr(view, "_fav_hover_star", False))
        # 悬停到热区时反转星标（实心↔空心）作为可点击反馈
        if star_over:
            star = "\u2606" if fav else "\u2605"
        else:
            star = "\u2605" if fav else "\u2606"
        return fav, star

    @staticmethod
    def _favorite_key_for_role(role):
        """把 item 的 UserRole 转成收藏 key：笔记 int / 外部与 IPC 文件 str / 其它 None。"""
        if isinstance(role, int) and not isinstance(role, bool):
            return int(role)
        if isinstance(role, str) and (
            role.startswith(EXTERNAL_FILE_PREFIX) or role.startswith(IPC_FILE_PREFIX)
        ):
            return role
        return None

    def paint(self, painter, option, index):
        # 先绘制背景/选中等默认样式
        opt_copy = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt_copy, index)
        opt_copy.text = ""
        style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt_copy, painter, option.widget)

        # 若 item 被标记为“父级高亮”，在默认背景之上叠加高亮色块
        is_highlighted = bool(index.data(self._PARENT_HIGHLIGHT_ROLE))
        if is_highlighted:
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.fillRect(option.rect, self._COLOR_PARENT_HIGHLIGHT)
            painter.restore()

        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        parts = text.split("\n") if isinstance(text, str) else [str(text)]
        rect = option.rect
        padding_left = 2
        total_lines = len(parts)
        # 紧凑行高：根据行数自适应
        line_height = max(14, rect.height() // max(total_lines, 1))
        x = rect.x() + padding_left
        y = rect.y() + max(2, (rect.height() - line_height * total_lines) // 2)
        for i, part in enumerate(parts):
            font = QtGui.QFont(option.font)
            if i == 0:
                # 第一行：文件名
                role = index.data(QtCore.Qt.ItemDataRole.UserRole) or ""
                if isinstance(role, str) and role.startswith("__folder__:"):
                    color = self._COLOR_FOLDER
                elif role == ASK_AI_ITEM_ID:
                    color = self._COLOR_ASK_AI
                elif (
                    isinstance(role, str)
                    and (
                        role.startswith(EXTERNAL_FILE_PREFIX)
                        or role.startswith(IPC_FILE_PREFIX)
                    )
                    and part.lstrip().startswith("✕")
                ):
                    # 文件已在硬盘上删除：告警色（不做 I/O，只认渲染时的 ✕ 前缀）
                    color = self._COLOR_MISSING
                else:
                    color = self._COLOR_MAIN
                font.setBold(True)
            else:
                color = self._COLOR_DATE
                font.setBold(False)
                if font.pointSize() and font.pointSize() > 8:
                    font.setPointSize(font.pointSize() - 2)
            painter.save()
            painter.setFont(font)
            painter.setPen(QtGui.QPen(color))
            line_rect = QtCore.QRect(x, y + i * line_height, rect.width() - padding_left - 4, line_height)
            painter.drawText(
                line_rect,
                QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft,
                part,
            )
            painter.restore()

        # 右侧收藏星标（参考 l_pyside6_uv StarDelegate）
        _fav, star = self._star_state(option, index)
        if star is not None:
            painter.save()
            painter.setPen(QtGui.QPen(self._COLOR_STAR))
            star_font = QtGui.QFont(option.font)
            star_font.setPointSize(14)
            painter.setFont(star_font)
            painter.drawText(
                option.rect.adjusted(0, 0, -8, 0),
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
                star,
            )
            painter.restore()

    def sizeHint(self, option, index):
        base = super().sizeHint(option, index)
        role = index.data(QtCore.Qt.ItemDataRole.UserRole) or ""
        if isinstance(role, str) and role.startswith("__folder__:"):
            return QtCore.QSize(base.width(), 20)
        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""
        lines = text.split("\n") if isinstance(text, str) else [str(text)]
        return QtCore.QSize(base.width(), max(32, len(lines) * 14 + 4))


class _NoteTreeProxyStyle(QtWidgets.QProxyStyle):
    """在深色主题下绘制树形层级指示线（虚线）。

    重写 PE_IndicatorBranch：
    - 先调用基类绘制折叠/展开箭头三角；
    - 再叠加绘制层级指示线（天蓝淡色虚线）。
    """

    _LINE_COLOR = QtGui.QColor(137, 221, 255, 70)

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QtWidgets.QStyle.PrimitiveElement.PE_IndicatorBranch:
            super().drawPrimitive(element, option, painter, widget)
            return

        flags = option.state
        rect = option.rect
        SF = QtWidgets.QStyle.StateFlag

        # State_Children: 有子节点（需要画箭头）
        # State_Open    : 展开
        # State_Sibling : 当前节点下方还有兄弟节点（垂直线需要继续向下）
        has_children = bool(flags & SF.State_Children)
        has_sibling_below = bool(flags & SF.State_Sibling)

        # 基类绘制箭头三角（仅对有子节点的项）
        super().drawPrimitive(element, option, painter, widget)

        if rect.width() <= 2 or rect.height() <= 2 or widget is None:
            return

        # 通过 indexAt 判断是否为根节点：根节点不画层级指示线
        pos = QtCore.QPoint(rect.center().x(), rect.center().y())
        index = widget.indexAt(pos)
        if not index.isValid():
            return
        is_root = not index.parent().isValid()
        if is_root:
            return

        painter.save()
        pen = QtGui.QPen(self._LINE_COLOR)
        pen.setStyle(QtCore.Qt.PenStyle.DotLine)
        pen.setWidth(1)
        painter.setPen(pen)

        mid_x = rect.x() + rect.width() // 2
        mid_y = rect.y() + rect.height() // 2

        # 1) 垂直连接线
        if has_sibling_below:
            # 有兄弟在下方：竖线贯穿整个 rect
            painter.drawLine(mid_x, rect.top(), mid_x, rect.bottom())
        else:
            # 无兄弟在下方（最后一项）：竖线仅上半部（L 形）
            painter.drawLine(mid_x, rect.top(), mid_x, mid_y)

        # 2) 水平连接线：子节点画从中部向右的横线
        painter.drawLine(mid_x, mid_y, rect.right(), mid_y)

        painter.restore()


class _VersionComboBox(QtWidgets.QComboBox):
    """版本历史下拉框：展开前发出信号以便重新拉取最新版本列表。"""

    aboutToShowPopup = QtCore.Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setMinimumContentsLength(60)
        self.view().setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)

    def showPopup(self) -> None:
        self.aboutToShowPopup.emit()
        view = self.view()
        view.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        fm = view.fontMetrics()
        max_w = 0
        for i in range(self.count()):
            max_w = max(max_w, fm.horizontalAdvance(self.itemText(i)))
        popup_w = max(self.width(), max_w + 96)
        screen = self.window().screen() if self.window() else QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            popup_w = min(popup_w, screen.availableGeometry().width() - 40)
        view.setMinimumWidth(popup_w)
        view.setMaximumWidth(popup_w)
        container = view.parentWidget()
        if container is not None:
            container.setMinimumWidth(popup_w)
            container.setMaximumWidth(popup_w)
        super().showPopup()
        container = view.parentWidget()
        if container is not None:
            container.setMinimumWidth(popup_w)
            container.setMaximumWidth(popup_w)
            container.resize(popup_w, container.height())


class RightPanel(QtWidgets.QWidget):
    """右侧面板（代码构建），每次重建都会创建全新实例，规避 PySide6 中
    QUiLoader 加载的 QMainWindow 被 C++ 层销毁导致的控件失效问题。

    对应 main_window.ui 中 splitter_main 右侧的 right_panel 部分。
    所有子控件均以属性暴露（title_edit、provider_combo、model_combo 等）。
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("right_panel")

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 10, 12, 10)

        # ---- 标题行：title_edit + provider_combo + label_model + model_combo + btn_refresh_models
        title_row = QtWidgets.QHBoxLayout()
        title_row.setSpacing(8)

        self.title_edit = QtWidgets.QLineEdit(self)
        self.title_edit.setObjectName("title_edit")
        self.title_edit.setPlaceholderText("标题")
        title_row.addWidget(self.title_edit)

        self.provider_combo = QtWidgets.QComboBox(self)
        self.provider_combo.setObjectName("provider_combo")
        self.provider_combo.setMinimumWidth(120)
        title_row.addWidget(self.provider_combo)

        self.label_model = QtWidgets.QLabel("模型:", self)
        self.label_model.setObjectName("label_model")
        title_row.addWidget(self.label_model)

        self.model_combo = QtWidgets.QComboBox(self)
        self.model_combo.setObjectName("model_combo")
        self.model_combo.setMinimumWidth(260)
        title_row.addWidget(self.model_combo)

        self.btn_refresh_models = QtWidgets.QPushButton("刷新模型", self)
        self.btn_refresh_models.setObjectName("btn_refresh_models")
        title_row.addWidget(self.btn_refresh_models)
        root.addLayout(title_row)

        # ---- token 统计行
        token_row = QtWidgets.QHBoxLayout()
        token_row.setSpacing(8)

        self.label_input_tokens = QtWidgets.QLabel("输入: 0 tokens", self)
        self.label_input_tokens.setObjectName("label_input_tokens")
        token_row.addWidget(self.label_input_tokens)

        self.label_output_tokens = QtWidgets.QLabel("输出: 0 tokens", self)
        self.label_output_tokens.setObjectName("label_output_tokens")
        token_row.addWidget(self.label_output_tokens)

        self.label_cost = QtWidgets.QLabel("费用: 价格未知", self)
        self.label_cost.setObjectName("label_cost")
        token_row.addWidget(self.label_cost)

        self.label_price_source = QtWidgets.QLabel("价格: 未加载", self)
        self.label_price_source.setObjectName("label_price_source")
        token_row.addWidget(self.label_price_source)

        token_row.addStretch()

        self.tab_count = QtWidgets.QLabel("Tab: 0", self)
        self.tab_count.setObjectName("tab_count")
        token_row.addWidget(self.tab_count)
        root.addLayout(token_row)

        # ---- 版本历史行（切换历史修改版本）
        version_row = QtWidgets.QHBoxLayout()
        version_row.setSpacing(8)
        self.label_version = QtWidgets.QLabel("📜 版本", self)
        self.label_version.setObjectName("label_version")
        version_row.addWidget(self.label_version)
        self.combo_version = _VersionComboBox(self)
        self.combo_version.setObjectName("combo_version")
        self.combo_version.setToolTip("查看并切换到该笔记/日志的历史保存版本")
        self.combo_version.setMinimumWidth(260)
        self.combo_version.setStyleSheet(_VERSION_COMBO_QSS)
        self.combo_version.addItem("📜 切换版本", None)
        version_row.addWidget(self.combo_version)

        # ---- 版本对比：两个版本下拉框 + 对比按钮（默认最新 vs 上一版）
        self.label_diff = QtWidgets.QLabel("⇄ 差异", self)
        self.label_diff.setObjectName("label_diff")
        version_row.addWidget(self.label_diff)

        self.combo_diff_left = QtWidgets.QComboBox(self)
        self.combo_diff_left.setObjectName("combo_diff_left")
        self.combo_diff_left.setToolTip("选择「旧」版本（对比基准）")
        self.combo_diff_left.setMinimumWidth(180)
        self.combo_diff_left.setStyleSheet(_VERSION_COMBO_QSS)
        version_row.addWidget(self.combo_diff_left)

        self.label_diff_arrow = QtWidgets.QLabel("→", self)
        self.label_diff_arrow.setObjectName("label_diff_arrow")
        version_row.addWidget(self.label_diff_arrow)

        self.combo_diff_right = QtWidgets.QComboBox(self)
        self.combo_diff_right.setObjectName("combo_diff_right")
        self.combo_diff_right.setToolTip("选择「新」版本")
        self.combo_diff_right.setMinimumWidth(180)
        self.combo_diff_right.setStyleSheet(_VERSION_COMBO_QSS)
        version_row.addWidget(self.combo_diff_right)

        self.btn_diff = QtWidgets.QPushButton("对比", self)
        self.btn_diff.setObjectName("btn_diff")
        self.btn_diff.setToolTip("并排对比两个版本的内容")
        version_row.addWidget(self.btn_diff)

        version_row.addStretch()
        root.addLayout(version_row)

        # ---- AI 内容面板
        self.ai_widget = QtWidgets.QWidget(self)
        self.ai_widget.setObjectName("ai_widget")
        ai_layout = QtWidgets.QVBoxLayout(self.ai_widget)
        ai_layout.setSpacing(0)
        ai_layout.setContentsMargins(0, 0, 0, 0)

        # ---- AI 标签页（含 Template 标签）
        self.ai_tabs = QtWidgets.QTabWidget(self.ai_widget)
        self.ai_tabs.setObjectName("ai_tabs")
        self.ai_tabs.setDocumentMode(True)
        self.ai_tabs.setTabsClosable(True)

        self._ai_tab_template_widget = QtWidgets.QWidget()
        self._ai_tab_template_widget.setObjectName("ai_tab_template")
        tpl = QtWidgets.QVBoxLayout(self._ai_tab_template_widget)
        tpl.setSpacing(10)
        tpl.setContentsMargins(10, 10, 10, 10)

        self._ai_template_content_edit = CodeEditorWidget(self._ai_tab_template_widget)
        self._ai_template_content_edit.setObjectName("CodeEditor")
        self._ai_template_content_edit.setPlaceholderText(
            '在这里输入问题，然后点击"问AI"。\n\n'
            "支持多会话与上下文记忆。\n支持模型：硅基流动 / 智谱 Zhipu。"
        )
        self._ai_template_content_edit.set_search_bar_y_offset(
            -self._ai_template_content_edit.search_bar_height()
        )
        tpl.addWidget(self._ai_template_content_edit)

        self._ai_template_answer_edit = CodeEditorWidget(self._ai_tab_template_widget)
        self._ai_template_answer_edit.setObjectName("AiAnswerViewer")
        self._ai_template_answer_edit.setReadOnly(True)
        self._ai_template_answer_edit.setPlaceholderText("AI 回答会显示在这里（支持多会话上下文）")
        self._ai_template_answer_edit.set_search_bar_y_offset(
            -self._ai_template_answer_edit.search_bar_height()
        )
        tpl.addWidget(self._ai_template_answer_edit)

        self.ai_tabs.addTab(self._ai_tab_template_widget, "Template")
        self.ai_tabs.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        ai_layout.addWidget(self.ai_tabs, 1)
        root.addWidget(self.ai_widget, 1)

        # ---- 日志/笔记内容面板
        self.log_widget = QtWidgets.QWidget(self)
        self.log_widget.setObjectName("log_widget")
        log_layout = QtWidgets.QVBoxLayout(self.log_widget)
        log_layout.setSpacing(0)
        log_layout.setContentsMargins(0, 0, 0, 0)

        # ---- content_edit（普通笔记 / 外部文件 / 空态）
        self.content_edit = CodeEditorWidget(self.log_widget)
        self.content_edit.setObjectName("content_edit")
        self.content_edit.setAcceptDrops(True)
        self.content_edit.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        # 搜索栏向上偏移一个自身高度，避免遮挡内容
        self.content_edit.set_search_bar_y_offset(-self.content_edit.search_bar_height())
        log_layout.addWidget(self.content_edit, 1)
        self.status_bar = QtWidgets.QLabel("未加载文件", self)
        self.status_bar.setObjectName("CodeEditorStatusBar")
        self.status_bar.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.status_bar.setStyleSheet(
            "QLabel#CodeEditorStatusBar {"
            " background-color: #0b111c; color: #9fb3c8;"
            " border-top: 1px solid #1c2637; padding: 2px 10px; font-size: 11px;"
            "}"
        )

        # ---- ai_answer_edit（旧版 AI 回答区，默认隐藏）
        self.ai_answer_edit = CodeEditorWidget(self)
        self.ai_answer_edit.setObjectName("ai_answer_edit")
        self.ai_answer_edit.setVisible(False)
        self.ai_answer_edit.setReadOnly(True)
        self.ai_answer_edit.setPlaceholderText("AI 回答会显示在这里")
        self.ai_answer_edit.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self.ai_answer_edit.set_search_bar_y_offset(-self.ai_answer_edit.search_bar_height())
        root.addWidget(self.ai_answer_edit, 1)
        root.addWidget(self.log_widget, 1)
        root.addWidget(self.status_bar)

        # ---- 操作按钮行
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_new = QtWidgets.QPushButton("新建", self)
        self.btn_new.setObjectName("btn_new")
        btn_row.addWidget(self.btn_new)

        self.btn_save = QtWidgets.QPushButton("保存", self)
        self.btn_save.setObjectName("btn_save")
        btn_row.addWidget(self.btn_save)

        self.btn_delete = QtWidgets.QPushButton("删除", self)
        self.btn_delete.setObjectName("btn_delete")
        btn_row.addWidget(self.btn_delete)

        btn_row.addStretch()

        self.btn_ai_clear = QtWidgets.QPushButton("清空上下文", self)
        self.btn_ai_clear.setObjectName("btn_ai_clear")
        self.btn_ai_clear.setToolTip("清空当前问AI 会话的对话历史，之后提问不再带上旧上下文")
        btn_row.addWidget(self.btn_ai_clear)

        self.btn_ai_ask = QtWidgets.QPushButton("问AI", self)
        self.btn_ai_ask.setObjectName("btn_ai_ask")
        self.btn_ai_ask.setToolTip(
            "发送输入框内容（输入框内回车即发送，Shift+回车换行）"
        )
        btn_row.addWidget(self.btn_ai_ask)
        root.addLayout(btn_row)

    # ---- 公共方法 -------------------------------------------------------

    def attach_to_splitter(self, splitter: QtWidgets.QSplitter) -> None:
        """把 right_panel 作为第二个子控件加入 splitter，并设置拉伸策略。"""
        self.setParent(splitter)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def bind_to(self, mw: "MainWindow") -> None:
        """将本实例的控件绑定到 MainWindow 属性，并记录到 _right_widget_refs。"""
        mapping = {
            "title_edit": self.title_edit,
            "provider_combo": self.provider_combo,
            "label_model": self.label_model,
            "model_combo": self.model_combo,
            "btn_refresh_models": self.btn_refresh_models,
            "label_input_tokens": self.label_input_tokens,
            "label_output_tokens": self.label_output_tokens,
            "label_cost": self.label_cost,
            "label_price_source": self.label_price_source,
            "tab_count": self.tab_count,
            "ai_widget": self.ai_widget,
            "log_widget": self.log_widget,
            "content_edit": self.content_edit,
            "CodeEditorStatusBar": self.status_bar,
            "ai_answer_edit": self.ai_answer_edit,
            "ai_tabs": self.ai_tabs,
            "btn_new": self.btn_new,
            "btn_save": self.btn_save,
            "btn_delete": self.btn_delete,
            "btn_ai_ask": self.btn_ai_ask,
            "btn_ai_clear": self.btn_ai_clear,
            "label_version": self.label_version,
            "combo_version": self.combo_version,
            "label_diff": self.label_diff,
            "combo_diff_left": self.combo_diff_left,
            "label_diff_arrow": self.label_diff_arrow,
            "combo_diff_right": self.combo_diff_right,
            "btn_diff": self.btn_diff,
        }
        for key, widget in mapping.items():
            setattr(mw, key, widget)
            mw._right_widget_refs[key] = widget

        # MainWindow 还需引用 ai_tab_template 相关的隐藏控件（供 ai 标签页新建复用）
        mw._ai_tab_template_widget = self._ai_tab_template_widget
        mw._ai_template_content_edit = self._ai_template_content_edit
        mw._ai_template_answer_edit = self._ai_template_answer_edit

        # 隐藏 Template 标签页并移除它（保留模板控件引用供后续使用）
        tpl_index = mw.ai_tabs.indexOf(self._ai_tab_template_widget)
        if tpl_index >= 0:
            mw.ai_tabs.removeTab(tpl_index)
        self._ai_tab_template_widget.hide()


class MainWindow(TrayAwareMixin, QtWidgets.QWidget):
    # 请求外层（自定义标题栏外壳）更新标题文本
    title_text_changed = QtCore.Signal(str)

    def __init__(self, api: NotepadApi, restart_callback=None, hotkey_interval_callback=None, hotkey_key_callback=None) -> None:
        super().__init__()
        self.api = api
        self._restart_callback = restart_callback
        self._hotkey_interval_callback = hotkey_interval_callback
        self._hotkey_key_callback = hotkey_key_callback
        self.state = UiState()
        self._allow_close = False
        self._settings = QtCore.QSettings("Lugwit", "l_notepad_pc")
        # 收藏顺序：笔记用 int(note_id)，外部/IPC 文件用带前缀的路径字符串
        self._favorite_order: list[int | str] = []
        # 手动排序：相对路径(note.title)的全局有序列表，持久化到配置目录的 note_order.json
        self._note_order: list[str] = []
        # 中键拖拽调序的临时状态
        self._mid_drag_src_title: str | None = None
        self._mid_drag_src_item: QtWidgets.QTreeWidgetItem | None = None
        self._mid_drag_start_pos: QtCore.QPoint | None = None
        self._mid_dragging = False
        self._drop_indicator: QtWidgets.QRubberBand | None = None
        self._drop_box: QtWidgets.QRubberBand | None = None
        self._last_open_note_id: int | None = None
        # 从设置中加载 AI 模型，如果保存过则使用保存的值
        saved_model = self._settings.value("ai/model", "")
        self._selected_ai_model = saved_model if saved_model else DEFAULT_SILICONFLOW_MODEL
        self._model_prices: dict[str, ModelPrice] = {}
        self._ai_input_tokens = 0
        self._ai_output_tokens = 0
        self._ai_stream_text = ""
        self._text_font_size = 10
        self._ask_ai_mode = False
        self._external_files: list[str] = []
        self._ipc_files: list[str] = []
        self._current_external_file: str | None = None
        self._current_ipc_file: str | None = None
        self._current_server_log_path: str | None = None
        # 本地文件（笔记 / 外部文件）外部变更检测：记录加载快照 + 文件监视/轮询
        self._loaded_local_file_path: str | None = None
        self._loaded_local_file_mtime: float | None = None
        self._loaded_local_file_size: int | None = None
        self._local_file_reload_prompting = False
        self._local_file_watcher = QtCore.QFileSystemWatcher(self)
        self._local_file_watcher.fileChanged.connect(self._on_local_file_watch_event)
        self._local_file_watcher.directoryChanged.connect(self._on_local_file_watch_event)
        # 轮询兜底：编辑器「原子替换」写盘会让 QFileSystemWatcher 丢目标，靠 mtime 复核
        self._local_file_poll_timer = QtCore.QTimer(self)
        self._local_file_poll_timer.setInterval(_LOCAL_FILE_POLL_MS)
        self._local_file_poll_timer.timeout.connect(self._poll_local_file_changed)
        self._ai_sessions: dict[str, AiSession] = {}
        self._current_ai_session_id: str | None = None
        self._ai_request_seq = 0
        self._active_ai_request_id: int | None = None
        self._in_selection_changed = False
        # 右键弹菜单期间：Qt 会把当前项挪到右键那一项并触发选中变化，
        # 用它挡住「右键即打开文件」，菜单收起后还原原选中项
        self._right_click_guard = False
        self._right_click_view = None
        self._right_click_selection: list = []
        self._initializing = True
        # 笔记列表后台加载（QThread）：进行中的加载器 + 待重载标记
        self._notes_loader: _NotesLoaderThread | None = None
        self._notes_reload_pending = False
        # 全文检索：去抖定时器 + 后台检索线程 + 命中结果
        # _search_hits=None 表示不在搜索模式（走原来的本地标题过滤）
        self._search_hits: list[dict] | None = None
        self._search_extra_hits: list[tuple[int, dict]] = []
        self._search_pending_text = ""
        self._search_reload_pending = False
        self._search_thread: _SearchThread | None = None
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._on_search_timeout)
        # 右侧区域控件引用统一收口，避免只依赖局部属性导致引用丢失
        self._right_widget_refs: dict[str, QtWidgets.QWidget] = {}
        self._tray_icon: QtWidgets.QSystemTrayIcon | None = None
        self._log_level = "INFO"
        self._settings.setValue("log/level", self._log_level)
        self._previous_excepthook = sys.excepthook
        sys.excepthook = self._log_unhandled_exception
        self._ai_bridge = AiBridge()
        self._ai_bridge.chunk.connect(self._on_ai_chunk)
        self._ai_bridge.reasoning_chunk.connect(self._on_ai_reasoning_chunk)
        self._ai_bridge.finished.connect(self._on_ai_finished)
        self._model_bridge = ModelBridge()
        self._model_bridge.finished.connect(self._on_models_loaded)
        self._price_bridge = PriceBridge()
        self._price_bridge.finished.connect(self._on_prices_loaded)
        self.setStyleSheet(_load_stylesheet())
        self.setWindowTitle("L Notepad")
        self.resize(980, 640)
        self._normal_width = 980  # 正常宽度，Ctrl+中键会缩小到 400px，双击 Ctrl 恢复
        icon_path = Path(__file__).resolve().parent / "static" / "favicon.svg"
        if icon_path.exists():
            self.setWindowIcon(QtGui.QIcon(str(icon_path)))
        self._load_settings()
        self._load_external_files_state()
        self._restore_window_state()

        ui_path = Path(__file__).resolve().parent / "main_window.ui"
        loader = QtUiTools.QUiLoader(self)
        try:
            loader.registerCustomWidget(FolderFavoritesPanel)
            loader.registerCustomWidget(AccountFavoritesPanel)
        except Exception:
            pass
        ui_file = QtCore.QFile(str(ui_path))
        if not ui_file.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"Failed to open ui file: {ui_path}")
        try:
            loaded = loader.load(ui_file, self)
        finally:
            ui_file.close()
        if loaded is None:
            raise RuntimeError(f"Failed to load ui file: {ui_path}")

        # 右侧面板由代码构建（RightPanel 类），每次切换文件时重建实例，
        # 彻底规避旧版本中 QWidget 失效的问题。
        _cw = loaded.findChild(QtWidgets.QWidget, "centralwidget")
        if _cw is None and loaded.objectName() in {"centralwidget", "MainWindowRoot"}:
            _cw = loaded
        if _cw is None:
            tabs = loaded.findChild(QtWidgets.QTabWidget, "tabs")
            if tabs is not None:
                _cw = loaded
        if _cw is None:
            child_names = [child.objectName() for child in loaded.findChildren(QtWidgets.QWidget)]
            raise RuntimeError(
                f"main_window.ui missing usable root widget; root={loaded.objectName()!r}; "
                f"children={child_names[:30]}"
            )
        # QWidget 基类无 setCentralWidget：用根布局承载 centralwidget，
        # 状态栏在 _setupUiComponents 中追加到该布局底部。
        self._root_layout = QtWidgets.QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)
        _cw.setParent(self)
        self._root_layout.addWidget(_cw)
        # loaded 的 C++ 根 QWidget 在 reparent centralwidget 后已不再需要；如果
        # centralwidget 就是根对象，不能删除 loaded，否则会把实际界面删掉。
        if loaded is not _cw:
            del loaded
            import gc as _gc
            _gc.collect()

        # 通过 self.findChild 查找全部控件（centralwidget 已 reparent 到 self）
        self.tabs = self.findChild(QtWidgets.QTabWidget, "tabs")
        self.search_edit = self.findChild(QtWidgets.QLineEdit, "search_edit")
        self.notes_list = self.findChild(QtWidgets.QListWidget, "notes_list")
        self.notes_tree = self.findChild(QtWidgets.QTreeWidget, "notes_list")
        self.btn_refresh = self.findChild(QtWidgets.QPushButton, "btn_refresh")
        self.btn_favorite = self.findChild(QtWidgets.QPushButton, "btn_favorite")
        self.log_view = self.findChild(QtWidgets.QTextEdit, "log_view")
        self.help_view = self.findChild(QtWidgets.QTextEdit, "help_view")

        splitter = self.findChild(QtWidgets.QSplitter, "splitter_main")
        if splitter is None:
            raise RuntimeError("main_window.ui missing splitter_main")

        # 移除 .ui 中原有的 right_panel（将由代码构建的 RightPanel 替代）
        for _i in range(splitter.count()):
            _w = splitter.widget(_i)
            if _w is not None and _w.objectName() == "right_panel":
                _w.setParent(None)
                _w.deleteLater()
                break

        # 代码构建右侧面板
        self._right_panel = RightPanel(self)
        splitter.addWidget(self._right_panel)
        splitter.setStretchFactor(0, 0)  # 左侧不拉伸（保持紧凑）
        splitter.setStretchFactor(1, 1)  # 右侧占满剩余空间
        # 初始宽度分配：左侧 ~180px，右侧按窗口宽度补
        try:
            total_w = splitter.width() or 900
            left_w = min(200, max(170, int(total_w * 0.18)))
            splitter.setSizes([left_w, max(100, total_w - left_w)])
        except Exception:
            splitter.setSizes([180, 720])
        self._right_panel.bind_to(self)

        # 用 CodeEditorWidget 替换 .ui 中的 QTextEdit（日志 / 帮助）
        self.log_view = replace_text_edit_with_code_editor(self.log_view, "LogViewer")
        if self.help_view is not None:
            self.help_view = replace_text_edit_with_code_editor(self.help_view, "HelpViewer")
            self.help_view.setReadOnly(True)
            self.help_view.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            self._load_help_page()

        required_widgets = [
            ("tabs", self.tabs),
            ("search_edit", self.search_edit),
            ("notes_list_or_tree", self.notes_list or self.notes_tree),
            ("title_edit", self.title_edit),
            ("provider_combo", self.provider_combo),
            ("label_model", self.label_model),
            ("model_combo", self.model_combo),
            ("btn_refresh_models", self.btn_refresh_models),
            ("label_input_tokens", self.label_input_tokens),
            ("label_output_tokens", self.label_output_tokens),
            ("label_cost", self.label_cost),
            ("label_price_source", self.label_price_source),
            ("tab_count", self.tab_count),
            ("ai_widget", self.ai_widget),
            ("log_widget", self.log_widget),
            ("content_edit", self.content_edit),
            ("CodeEditorStatusBar", self._right_widget_refs.get("CodeEditorStatusBar")),
            ("ai_answer_edit", self.ai_answer_edit),
            ("btn_new", self.btn_new),
            ("btn_save", self.btn_save),
            ("btn_delete", self.btn_delete),
            ("btn_refresh", self.btn_refresh),
            ("btn_favorite", self.btn_favorite),
            ("btn_ai_ask", self.btn_ai_ask),
            ("log_view", self.log_view),
            ("ai_tabs", self.ai_tabs),
        ]
        missing = [name for name, w in required_widgets if w is None]
        if missing:
            raise RuntimeError(
                f"main_window.ui missing required widget objectName(s): {missing}\n"
                f"  ui_path: {ui_path} (exists={ui_path.exists()})"
            )
        assert self.tab_count is not None
        self.tab_count.setText("Tab: 0")

        # 初始化 .ui 文件中的 "问AI" item data
        self._notes_tree_mode = self._qt_is_valid(getattr(self, "notes_tree", None))
        if self._notes_tree_mode:
            self._setup_notes_tree()
        else:
            if self.notes_list and self.notes_list.count() > 0:
                first_item = self.notes_list.item(0)
                if first_item.text().startswith("问AI"):
                    first_item.setData(QtCore.Qt.ItemDataRole.UserRole, ASK_AI_ITEM_ID)
                    font = first_item.font()
                    font.setBold(True)
                    first_item.setFont(font)
                    first_item.setFlags(first_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsDragEnabled)
                    first_item.setSizeHint(QtCore.QSize(0, 36))

            if self.notes_list:
                self.notes_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            self.notes_list.itemSelectionChanged.connect(self._on_selection_changed)
            self.notes_list.itemDoubleClicked.connect(self._rename_note_from_item)
            self.notes_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            self.notes_list.customContextMenuRequested.connect(self._on_notes_list_context_menu)
            self.notes_list.model().rowsMoved.connect(self._on_notes_rows_moved)
            # 接收资源管理器拖入的文件 → 归类「外部文件」分组
            self.notes_list.setAcceptDrops(True)
            self.notes_list.viewport().setAcceptDrops(True)
            self.notes_list.viewport().installEventFilter(self)

        self.search_edit.textChanged.connect(self._apply_filter)

        # 右侧面板信号连接（初始 + 切换文件时重建都会调用 _connect_right_panel_signals）
        self._connect_right_panel_signals()

        self._tab_main_widget = self.findChild(QtWidgets.QWidget, "tab_main")
        self._tab_log_widget = self.findChild(QtWidgets.QWidget, "tab_log")
        self._folder_favorites_panel = self.findChild(FolderFavoritesPanel, "tab_folder_favorites")
        if self._folder_favorites_panel is None:
            self._folder_favorites_panel = FolderFavoritesPanel(self, restart_callback=self._restart_app)
        else:
            self._folder_favorites_panel._restart_callback = self._restart_app
            if hasattr(self._folder_favorites_panel, "finalize_ui"):
                self._folder_favorites_panel.finalize_ui()
        # 收藏夹识别到调用程序/路径后，转发给外层更新自定义标题栏
        if hasattr(self._folder_favorites_panel, "caller_info_changed"):
            self._folder_favorites_panel.caller_info_changed.connect(self.title_text_changed)
            # 原生标题栏模式（l_notepad_ori）下本窗口即顶层，直接更新窗口标题
            self.title_text_changed.connect(self.setWindowTitle)
        self._folder_favorites_tab_index = -1
        
        # 命令收藏标签页
        self._command_favorites_panel = self.findChild(FolderFavoritesPanel, "tab_command_favorites")
        if self._command_favorites_panel is None:
            self._command_favorites_panel = FolderFavoritesPanel(self, restart_callback=self._restart_app)
            self._command_favorites_panel.set_favorites_kind("command")
        else:
            self._command_favorites_panel._restart_callback = self._restart_app
            self._command_favorites_panel.set_favorites_kind("command")
            if hasattr(self._command_favorites_panel, "finalize_ui"):
                self._command_favorites_panel.finalize_ui()
        self._command_favorites_tab_index = -1

        # 网址收藏标签页
        self._url_favorites_panel = self.findChild(FolderFavoritesPanel, "tab_url_favorites")
        if self._url_favorites_panel is None:
            self._url_favorites_panel = FolderFavoritesPanel(self, restart_callback=self._restart_app)
            self._url_favorites_panel.set_favorites_kind("url")
        else:
            self._url_favorites_panel._restart_callback = self._restart_app
            self._url_favorites_panel.set_favorites_kind("url")
            if hasattr(self._url_favorites_panel, "finalize_ui"):
                self._url_favorites_panel.finalize_ui()
        self._url_favorites_tab_index = -1
        
        # 延迟创建 SettingsWidget，在首次打开设置弹窗时才实例化
        self._settings_widget: SettingsWidget | None = None
        if self.tabs is not None:
            if self.tabs.indexOf(self._folder_favorites_panel) < 0:
                self.tabs.insertTab(1, self._folder_favorites_panel, " 文件夹")
            self._folder_favorites_tab_index = self.tabs.indexOf(self._folder_favorites_panel)

            if self.tabs.indexOf(self._command_favorites_panel) < 0:
                self.tabs.insertTab(2, self._command_favorites_panel, "⚡ 命令")
            self._command_favorites_tab_index = self.tabs.indexOf(self._command_favorites_panel)
            
            if self.tabs.indexOf(self._url_favorites_panel) < 0:
                self.tabs.insertTab(3, self._url_favorites_panel, "🌐 网址")
            self._url_favorites_tab_index = self.tabs.indexOf(self._url_favorites_panel)
            
            # 标签栏右键菜单（支持文件夹收藏和网址收藏）
            _tab_bar = self.tabs.tabBar()
            _tab_bar.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            _tab_bar.customContextMenuRequested.connect(self._on_tab_bar_context_menu)
            # 重启按钮放在标签栏右上角
            self.btn_restart = QtWidgets.QPushButton("重启", self)
            self.btn_restart.setFixedHeight(20)
            self.btn_restart.setObjectName("btn_restart")
            self.btn_restart.clicked.connect(self._restart_app)
            self.tabs.setCornerWidget(self.btn_restart, QtCore.Qt.Corner.TopRightCorner)
            # 信号连接延迟到首次打开设置弹窗时执行
            self.tabs.currentChanged.connect(self._on_main_tab_changed)

        self.log_view.setObjectName("LogViewer")
        self._apply_text_font_size()
        # log_view / help_view 的事件过滤（右侧面板的 content_edit / ai_answer_edit
        # 已在 _connect_right_panel_signals 中处理）
        for editor_widget in (self.log_view, self.help_view):
            if editor_widget is None:
                continue
            if hasattr(editor_widget, "editor"):
                editor = editor_widget.editor()
                editor.installEventFilter(self)
                editor.viewport().installEventFilter(self)

        # QWidget 基类无 statusBar()：手动创建 QStatusBar 并置于根布局底部
        self.status = QtWidgets.QStatusBar(self)
        self._root_layout.addWidget(self.status)

        self._log_file_path_label = QtWidgets.QLabel("日志路径: 未设置")
        self._console_log_path: str = ""
        self._console_log_tailer = None
        self._console_log_offset = 0
        self._console_log_buffer = ""
        self._console_log_path: str | None = None
        self._log_file_path_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._log_file_path_label.setToolTip("当前日志文件路径")
        # 日志路径显示在「日志」标签页内部顶部，而非状态栏
        if self._tab_log_widget is not None and self._tab_log_widget.layout() is not None:
            self._tab_log_widget.layout().insertWidget(0, self._log_file_path_label)

        # 状态栏添加当前高亮模式显示
        self._highlight_mode_label = QtWidgets.QLabel("高亮: 日志文件")
        self.status.addPermanentWidget(self._highlight_mode_label)

        # 应用默认高亮模式（注意：需要在 _highlight_mode_label 创建之后调用）
        self._on_highlight_mode_changed("日志文件")
        
        self._refresh_official_prices()
        # 初始化账号收藏
        self._init_account_favorites()
        # 恢复 AI 标签页
        self._restore_ai_tabs()
        # 初始状态下隐藏 AI 相关控件（因为 _ask_ai_mode = False）
        if self.provider_combo:
            self.provider_combo.hide()
        if self.label_model:
            self.label_model.hide()
        if self.model_combo:
            self.model_combo.hide()
        if self.btn_refresh_models:
            self.btn_refresh_models.hide()
        if self.label_input_tokens:
            self.label_input_tokens.hide()
        if self.label_output_tokens:
            self.label_output_tokens.hide()
        if self.label_cost:
            self.label_cost.hide()
        if self.label_price_source:
            self.label_price_source.hide()
        self.refresh_notes()
        self._initializing = False
        self._append_console_line("日志窗口已初始化")
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._save_settings)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self._wait_notes_loader)

    def _mode_from_filename(self, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        ext_mode_map = {
            ".py": "python",
            ".md": "markdown_preview",
            ".mdc": "markdown_preview",
            ".markdown": "markdown_preview",
            ".log": "log",
            ".txt": "text",
        }
        return ext_mode_map.get(ext, "text")

    def _diff_mode_from_filename(self, filename: str) -> str:
        """对比窗口用的编辑模式：必须源码态，行号才能与源码 1:1 对齐。"""
        mode = self._mode_from_filename(filename)
        # markdown_preview 会把多行渲染成块（表格/图片），行号无法与源码对应
        return "markdown" if mode == "markdown_preview" else mode

    def _note_file_path(self, title: str) -> Path:
        return self._notepad_list_dir() / title

    @staticmethod
    def _file_missing(file_path: str) -> bool:
        """文件在硬盘上是否已不存在（列表 ✕ 标记、刷新清理共用）。"""
        try:
            return not Path(file_path).is_file()
        except OSError:
            return True

    def _file_entry_marker(self, file_path: str) -> tuple[str, bool]:
        """文件条目的箭头/删除标记：硬盘上已不存在时用 ✕，返回 (标记, 是否已删除)。"""
        missing = self._file_missing(file_path)
        return ("✕" if missing else "↗"), missing

    def _file_entry_tooltip(self, file_path: str, missing: bool) -> str:
        if missing:
            return f"{file_path}\n文件已不存在（硬盘上已删除）——点底部「刷新」清理该条目"
        return file_path

    @staticmethod
    def _missing_entry_color() -> QtGui.QColor:
        return QtGui.QColor("#FF6B6B")

    def _item_local_file_path(self, item_id, title: str | None = None) -> Path | None:
        """笔记 / 外部 / IPC 文件条目 → 磁盘文件路径；其它条目返回 None。"""
        if isinstance(item_id, int) and not isinstance(item_id, bool):
            if not title:
                return None
            return self._note_file_path(title)
        if isinstance(item_id, str):
            if item_id.startswith(EXTERNAL_FILE_PREFIX):
                return Path(item_id[len(EXTERNAL_FILE_PREFIX):])
            if item_id.startswith(IPC_FILE_PREFIX):
                return Path(item_id[len(IPC_FILE_PREFIX):])
        return None

    def _copy_text_to_clipboard(self, text: str, *, label: str = "") -> None:
        """复制文本到剪贴板并给状态栏反馈（空内容忽略）。"""
        value = str(text or "")
        if not value:
            self.status.showMessage("没有可复制的内容", 2500)
            return
        try:
            clipboard_recorder.write_to_clipboard(text=value)
        except Exception as exc:
            lprint(f"复制到剪贴板失败：{exc}")
            self.status.showMessage(f"复制失败：{exc}", 4000)
            return
        self.status.showMessage(f"已复制{label}：{value}", 3000)

    def _open_path_in_explorer(self, path: Path) -> None:
        """在资源管理器中定位文件（选中）或打开文件夹。"""
        try:
            if path.is_dir():
                target_dir = path
                select = None
            elif path.exists():
                target_dir = path.parent
                select = path
            else:
                # 文件已被删除/移动：退到最近的已存在父目录，避免 Explorer 弹错误框
                parent = path.parent
                while parent != parent.parent and not parent.exists():
                    parent = parent.parent
                if not parent.exists():
                    self.status.showMessage(f"文件不存在：{path}", 4000)
                    return
                target_dir, select = parent, None
        except OSError:
            self.status.showMessage(f"文件不存在：{path}", 4000)
            return
        try:
            if sys.platform == "win32":
                if select is not None:
                    subprocess.Popen(["explorer", "/select,", str(select)])
                else:
                    subprocess.Popen(["explorer", str(target_dir)])
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target_dir)))
        except Exception as exc:
            lprint(f"打开所在文件夹失败：{path} ({exc})")
            self.status.showMessage(f"打开所在文件夹失败：{exc}", 4000)
            return
        self.status.showMessage(f"已在资源管理器中打开：{target_dir}", 2500)

    def _on_tab_bar_context_menu(self, pos) -> None:
        """右键标签栏时弹出操作菜单（支持文件夹收藏和网址收藏）。"""
        if self.tabs is None:
            return
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        
        # 检查是否是文件夹收藏标签页
        if self._folder_favorites_panel and self.tabs.widget(idx) is self._folder_favorites_panel:
            self._folder_favorites_panel.show_actions_menu(bar.mapToGlobal(pos))
            return

        # 检查是否是命令收藏标签页
        if self._command_favorites_panel and self.tabs.widget(idx) is self._command_favorites_panel:
            self._command_favorites_panel.show_actions_menu(bar.mapToGlobal(pos))
            return
        
        # 检查是否是网址收藏标签页
        if self._url_favorites_panel and self.tabs.widget(idx) is self._url_favorites_panel:
            menu = QtWidgets.QMenu(self)
            menu.addAction(" 添加网址").triggered.connect(self._url_favorites_panel._add_url)
            menu.exec(bar.mapToGlobal(pos))
            return

    def _on_main_tab_changed(self, index: int) -> None:
        if index < 0 or self.log_view is None or self._tab_log_widget is None:
            return
        if self.tabs.widget(index) is not self._tab_log_widget:
            return
        self.log_view.restore_text_from_cache(LOG_VIEW_CONTENT_CACHE_KEY, mode="log")

    def _on_highlight_mode_changed(self, mode_text: str) -> None:
        """切换内容编辑器的高亮模式。
        
        Args:
            mode_text: 模式名称
        """
        mode_map = {
            "普通文本": "text",
            "Python 代码": "python",
            "Markdown 源码": "markdown",
            "日志文件": "log"
        }
        mode = mode_map.get(mode_text, "log")
        
        # 使用 CodeEditorWidget 的 set_mode 方法切换模式
        self.content_edit.set_mode(mode)
        
        # 同步设置日志视图为 log 模式
        self.log_view.set_mode("log")
        
        # 更新状态栏
        self._current_highlight_mode = mode
        self._highlight_mode_label.setText(f"高亮: {mode_text}")
        self.status.showMessage(f"已切换到 {mode_text} 模式", 2000)

    def _auto_set_highlight_mode(self, filename: str) -> None:
        """根据文件扩展名自动设置高亮模式。
        
        Args:
            filename: 文件名
        """
        ext = Path(filename).suffix.lower()
        
        # 扩展名到模式的映射
        ext_mode_map = {
            '.py': ('python', 'Python 代码'),
            '.md': ('markdown_preview', 'Markdown 预览'),
            '.mdc': ('markdown_preview', 'Markdown 预览'),
            '.markdown': ('markdown_preview', 'Markdown 预览'),
            '.log': ('log', '日志文件'),
            '.txt': ('text', '普通文本'),
        }
        
        mode, mode_text = ext_mode_map.get(ext, ('text', '普通文本'))
        
        # 设置编辑器模式
        self.content_edit.set_mode(mode)
        
        # 更新状态栏显示
        self._current_highlight_mode = mode
        self._highlight_mode_label.setText(f"高亮: {mode_text}")
        
        # 同步更新下拉框显示
        if hasattr(self, '_highlight_mode_combo'):
            index = self._highlight_mode_combo.findText(mode_text)
            if index >= 0:
                self._highlight_mode_combo.setCurrentIndex(index)

    def _notes_loader_running(self) -> bool:
        """后台加载器是否仍在运行（C++ 对象可能已被 deleteLater 销毁）。"""
        loader = getattr(self, "_notes_loader", None)
        if loader is None:
            return False
        try:
            return loader.isRunning()
        except RuntimeError:
            self._notes_loader = None
            return False

    def refresh_notes(self) -> None:
        # 笔记列表在 QThread 中加载（本地目录扫描 / 远程 HTTP），
        # 完成后经信号回主线程刷新，避免阻塞 UI（尤其启动首帧与搜索过滤）。
        if self._notes_loader_running():
            self._notes_reload_pending = True
            return
        loader = _NotesLoaderThread(self.api)
        loader.loaded.connect(self._on_notes_loaded)
        loader.finished.connect(loader.deleteLater)
        loader.finished.connect(self._on_notes_loader_finished)
        self._notes_loader = loader
        loader.start()

    def _on_notes_loaded(self, notes, error) -> None:
        if error is not None:
            self._show_error(str(error))
        elif notes is not None:
            self._populate_notes(notes)

    def _on_notes_loader_finished(self) -> None:
        # 线程结束后再冲刷待重载标记（此时 isRunning() 已为 False）
        self._notes_loader = None
        if self._notes_reload_pending:
            self._notes_reload_pending = False
            self.refresh_notes()

    def _wait_notes_loader(self) -> None:
        """退出前等待后台加载线程结束，避免 QThread 析构告警。"""
        loader = getattr(self, "_notes_loader", None)
        if loader is None:
            return
        try:
            if loader.isRunning():
                loader.wait(2000)
        except RuntimeError:
            # C++ 对象已被 deleteLater 销毁，说明线程早已结束
            self._notes_loader = None

    def _populate_notes(self, notes) -> None:
        current_id = None if self._current_external_file or self._current_ipc_file or self._current_server_log_path else self.state.current_note_id
        if current_id is None and not self._current_external_file and not self._current_ipc_file and not self._current_server_log_path:
            current_id = self._last_open_note_id
        query = self.search_edit.text().strip()
        notes_sorted = self._sort_notes(notes)
        if self._search_hits is not None:
            # 搜索模式：后端已给出命中（模糊 + 倒排），本地标题过滤不再生效。
            # 按命中顺序把 hits[].path 映射到 NoteDto，只渲染命中的笔记；
            # 映射不到的 note 命中 / kb 命中作为额外行（浏览器打开）。
            by_title = {n.title: n for n in notes}
            matched: list[NoteDto] = []
            extra: list[tuple[int, dict]] = []
            for idx, hit in enumerate(self._search_hits):
                note = by_title.get(hit.get("path")) if hit.get("source") == "note" else None
                if note is not None and not note.title.startswith("问AI"):
                    matched.append(note)
                else:
                    extra.append((idx, hit))
            self._search_extra_hits = extra
            grouped_notes = self._group_notes_by_folder(matched, query="")
            render_query = ""
        else:
            self._search_extra_hits = []
            grouped_notes = self._group_notes_by_folder(notes_sorted, query=query)
            render_query = query
        if self._notes_tree_mode:
            self._refresh_notes_tree(grouped_notes, query=render_query)
        else:
            self._refresh_notes_list(grouped_notes, query=render_query)

        if self._ask_ai_mode:
            self._set_ai_editor(self._current_ai_session_id)
        elif current_id is not None:
            selected = self._select_note_id(current_id)
            if not selected:
                lprint(f"刷新列表后未找到当前笔记: #{current_id}")
        elif self._current_external_file:
            selected = self._select_external_file(self._current_external_file)
            if not selected:
                lprint(f"刷新列表后未找到外部文件: {self._current_external_file}")
        elif self._current_ipc_file:
            selected = self._select_ipc_file(self._current_ipc_file)
            if not selected:
                lprint(f"刷新列表后未找到 IPC 文件: {self._current_ipc_file}")
        elif self._current_server_log_path:
            # 服务器日志保持当前编辑状态，无需重新选择
            pass
        elif self._notes_count() > 1:
            self._select_first_note_item()
        else:
            self._set_editor(None)
        self._update_favorite_button_label()

    def _on_refresh_button_clicked(self) -> None:
        """底部「刷新」：重扫笔记，并清理硬盘上已删除的外部/IPC 文件条目。"""
        removed = self._purge_missing_file_entries()
        self.refresh_notes()
        if removed:
            self.status.showMessage(
                f"已清理 {removed} 个硬盘上已删除的文件条目", 4000
            )
        else:
            self.status.showMessage("列表已刷新", 2000)

    def _purge_missing_file_entries(self) -> int:
        """移除硬盘上已不存在的外部/IPC 文件条目；返回清理数量。

        列表里的 ✕ 条目（文件被外部删除）在点「刷新」时一并清掉，
        当前正在查看的文件若被清理则回到空态/最近的笔记。
        """
        removed = 0
        for attr in ("_external_files", "_ipc_files"):
            entries = list(getattr(self, attr, None) or [])
            if not entries:
                continue
            kept = [p for p in entries if not self._file_missing(p)]
            if len(kept) != len(entries):
                gone = [p for p in entries if p not in kept]
                removed += len(gone)
                setattr(self, attr, kept)
                lprint(f"[清理已删除文件] {attr}: {gone}")
        if not removed:
            return 0
        if self._current_external_file and self._current_external_file not in self._external_files:
            self._current_external_file = None
        if self._current_ipc_file and self._current_ipc_file not in self._ipc_files:
            self._current_ipc_file = None
        self._save_external_files_state()
        return removed

    def _apply_filter(self) -> None:
        # 搜索框：去抖 350ms 后异步调用后端全文检索（模糊 + 倒排索引）；
        # 空串回到原行为（清空搜索态 + 重新加载笔记列表）。
        text = self.search_edit.text().strip()
        if not text:
            self._search_timer.stop()
            self._search_pending_text = ""
            self._search_hits = None
            self._search_extra_hits = []
            self.refresh_notes()
            return
        self._search_pending_text = text
        self._search_timer.start(_SEARCH_DEBOUNCE_MS)

    def _search_thread_running(self) -> bool:
        """后台检索线程是否仍在运行（C++ 对象可能已被 deleteLater 销毁）。"""
        thread = getattr(self, "_search_thread", None)
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._search_thread = None
            return False

    def _on_search_timeout(self) -> None:
        """去抖到期：启动后台检索线程。"""
        query = (self._search_pending_text or "").strip()
        if not query:
            return
        if self._search_thread_running():
            self._search_reload_pending = True
            return
        thread = _SearchThread(self.api, query, limit=200)
        thread.loaded.connect(self._on_search_loaded)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_search_thread_finished)
        self._search_thread = thread
        thread.start()

    def _on_search_loaded(self, result, error) -> None:
        if error is not None:
            # 检索失败：回退到原来的本地标题过滤（保持 _search_hits 为 None）
            self._search_hits = None
            self._search_extra_hits = []
            self._show_error(str(error))
        else:
            data = result or {}
            self._search_hits = list(data.get("hits") or [])
            total = data.get("total", len(self._search_hits))
            msg = f"搜索命中 {total} 条"
            if data.get("fallback"):
                msg += "（未找到精确短语，已显示模糊结果）"
            self.status.showMessage(msg, 3000)
        self.refresh_notes()

    def _on_search_thread_finished(self) -> None:
        # 线程结束后再冲刷待重载标记（此时 isRunning() 已为 False）
        self._search_thread = None
        if self._search_reload_pending:
            self._search_reload_pending = False
            self._on_search_timeout()

    def _search_hit_base_url(self) -> str:
        """全文检索命中打开用的入口前缀：server_config 入口去掉 /note 等后缀。"""
        try:
            base = server_config.api_url() or ""
        except Exception:
            base = getattr(self.api, "base_url", "") or ""
        for suffix in ("/note",):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base.rstrip("/")

    def _open_search_hit(self, item_id: str) -> None:
        """双击检索命中行：在默认浏览器打开后端给出的网页。"""
        try:
            idx = int(item_id[len(SEARCH_HIT_PREFIX):])
            hit = self._search_hits[idx]
        except (ValueError, TypeError, IndexError):
            return
        open_url = (hit or {}).get("open_url") or ""
        if not open_url:
            return
        base = self._search_hit_base_url()
        url = (base + open_url) if base else open_url
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            lprint(f"打开检索命中失败: {exc}")

    def _search_hit_label(self, hit: dict) -> str:
        """检索命中行显示文本：优先相对路径 rel，其次 path。"""
        return hit.get("rel") or hit.get("path") or ""

    def _append_search_hit_rows_list(self, lst) -> None:
        """搜索模式下，把知识库命中/未映射笔记命中作为额外行追加到列表末尾。

        UserRole 必须是字符串前缀（__searchhit__:<index>），否则会被当成笔记 id。
        """
        for idx, hit in self._search_extra_hits:
            item = QtWidgets.QListWidgetItem(f"  🔗 {self._search_hit_label(hit)}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, f"{SEARCH_HIT_PREFIX}{idx}")
            item.setToolTip(hit.get("snippet") or "")
            item.setForeground(QtGui.QBrush(QtGui.QColor(120, 170, 220)))
            item.setSizeHint(QtCore.QSize(0, 32))
            lst.addItem(item)

    def _append_search_hit_rows_tree(self, tree) -> None:
        """搜索模式下，把知识库命中/未映射笔记命中作为额外行追加到树末尾。

        UserRole 必须是字符串前缀（__searchhit__:<index>），否则会被当成笔记 id。
        """
        for idx, hit in self._search_extra_hits:
            item = QtWidgets.QTreeWidgetItem([f"  🔗 {self._search_hit_label(hit)}"])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, f"{SEARCH_HIT_PREFIX}{idx}")
            item.setToolTip(0, hit.get("snippet") or "")
            item.setForeground(0, QtGui.QBrush(QtGui.QColor(120, 170, 220)))
            tree.addTopLevelItem(item)

    def _setup_notes_tree(self) -> None:
        tree = self.notes_tree
        if tree is None:
            return
        tree.setHeaderHidden(True)
        tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setAnimated(True)
        tree.setExpandsOnDoubleClick(True)
        tree.setUniformRowHeights(False)
        tree.setIndentation(5)
        # 只保留第 0 列（名称 + 日期），隐藏其它列
        for col in range(1, tree.columnCount()):
            tree.setColumnHidden(col, True)
        # 自定义 delegate 实现两行不同颜色 + 右侧收藏星标
        if getattr(self, "_note_tree_delegate", None) is None:
            self._note_tree_delegate = _NoteTreeItemDelegate(tree, self)
        tree.setItemDelegate(self._note_tree_delegate)
        # 收藏星标需要鼠标悬停反馈：开启 viewport 的鼠标跟踪
        tree.setMouseTracking(True)
        tree.viewport().setMouseTracking(True)
        # 单列拉伸到面板右缘，保证右侧收藏星标贴在列表最右侧（与参考实现一致）
        header = tree.header()
        if header is not None:
            header.setStretchLastSection(True)
            try:
                header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            except Exception:
                pass
        # 自定义 ProxyStyle 绘制层级指示线
        if getattr(self, "_note_tree_style", None) is None:
            self._note_tree_style = _NoteTreeProxyStyle(tree.style())
        tree.setStyle(self._note_tree_style)
        tree.itemSelectionChanged.connect(self._on_selection_changed)
        tree.itemDoubleClicked.connect(self._rename_note_from_item)
        tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_notes_tree_context_menu)
        tree.itemExpanded.connect(self._on_folder_expanded_or_collapsed)
        tree.itemCollapsed.connect(self._on_folder_expanded_or_collapsed)
        # 中键拖拽调序：监听 viewport 鼠标/拖放事件（自管拖影 + 落点线）
        tree.viewport().setAcceptDrops(True)
        tree.viewport().installEventFilter(self)

    def _notes_count(self) -> int:
        if self._notes_tree_mode and self.notes_tree is not None:
            count = 0
            root = self.notes_tree.invisibleRootItem()
            for i in range(root.childCount()):
                child = root.child(i)
                count += self._count_note_items_recursive(child)
            return count
        return self.notes_list.count() if self.notes_list is not None else 0

    def _count_note_items_recursive(self, item: QtWidgets.QTreeWidgetItem) -> int:
        item_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        count = 1 if isinstance(item_id, int) else 0
        for i in range(item.childCount()):
            count += self._count_note_items_recursive(item.child(i))
        return count

    def _refresh_notes_list(self, grouped_notes: list[tuple[str, list[NoteDto]]], *, query: str = "") -> None:
        self.notes_list.blockSignals(True)
        for i in range(self.notes_list.count() - 1, -1, -1):
            item = self.notes_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) != ASK_AI_ITEM_ID:
                self.notes_list.takeItem(i)
        if grouped_notes:
            for folder_name, folder_items in grouped_notes:
                folder_label = folder_name or "未归档"
                folder_header = QtWidgets.QListWidgetItem(f"📁 {folder_label}")
                folder_header.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                folder_header.setData(QtCore.Qt.ItemDataRole.UserRole, f"__folder__:{folder_label}")
                folder_font = folder_header.font()
                folder_font.setBold(True)
                folder_header.setFont(folder_font)
                folder_header.setBackground(QtGui.QBrush(QtGui.QColor(245, 245, 245)))
                folder_header.setForeground(QtGui.QBrush(QtGui.QColor(90, 90, 90)))
                folder_header.setSizeHint(QtCore.QSize(0, 28))
                self.notes_list.addItem(folder_header)
                for n in folder_items:
                    self.notes_list.addItem(self._create_note_list_item(n))
        elif query:
            empty_item = QtWidgets.QListWidgetItem(f"未找到匹配笔记：{query}")
            empty_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            empty_item.setData(QtCore.Qt.ItemDataRole.UserRole, "__empty__")
            empty_item.setForeground(QtGui.QBrush(QtGui.QColor(120, 120, 120)))
            empty_item.setSizeHint(QtCore.QSize(0, 28))
            self.notes_list.addItem(empty_item)
        if self._external_files:
            ext_folder = QtWidgets.QListWidgetItem("📁 外部文件")
            ext_folder.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            ext_folder.setData(QtCore.Qt.ItemDataRole.UserRole, "__external_folder__")
            ext_font = ext_folder.font()
            ext_font.setBold(True)
            ext_folder.setFont(ext_font)
            ext_folder.setBackground(QtGui.QBrush(QtGui.QColor(245, 245, 245)))
            ext_folder.setForeground(QtGui.QBrush(QtGui.QColor(90, 90, 90)))
            ext_folder.setSizeHint(QtCore.QSize(0, 28))
            self.notes_list.addItem(ext_folder)
            for file_path in self._external_files:
                path = Path(file_path)
                if query and query.lower() not in path.name.lower():
                    continue
                mark, missing = self._file_entry_marker(file_path)
                item = QtWidgets.QListWidgetItem(f"  {mark} {path.name}\n  {file_path}")
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, f"{EXTERNAL_FILE_PREFIX}{file_path}")
                item.setToolTip(self._file_entry_tooltip(file_path, missing))
                if missing:
                    item.setForeground(QtGui.QBrush(self._missing_entry_color()))
                item.setSizeHint(QtCore.QSize(0, 44))
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.notes_list.addItem(item)
        if self._ipc_files:
            ipc_folder = QtWidgets.QListWidgetItem("📁 IPC 文件")
            ipc_folder.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            ipc_folder.setData(QtCore.Qt.ItemDataRole.UserRole, IPC_FILE_FOLDER_ID)
            ipc_font = ipc_folder.font()
            ipc_font.setBold(True)
            ipc_folder.setFont(ipc_font)
            ipc_folder.setBackground(QtGui.QBrush(QtGui.QColor(245, 245, 245)))
            ipc_folder.setForeground(QtGui.QBrush(QtGui.QColor(90, 90, 90)))
            ipc_folder.setSizeHint(QtCore.QSize(0, 28))
            self.notes_list.addItem(ipc_folder)
            for file_path in self._ipc_files:
                path = Path(file_path)
                if query and query.lower() not in path.name.lower():
                    continue
                mark, missing = self._file_entry_marker(file_path)
                item = QtWidgets.QListWidgetItem(f"  {mark} {path.name}\n  {file_path}")
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, f"{IPC_FILE_PREFIX}{file_path}")
                item.setToolTip(self._file_entry_tooltip(file_path, missing))
                if missing:
                    item.setForeground(QtGui.QBrush(self._missing_entry_color()))
                item.setSizeHint(QtCore.QSize(0, 44))
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.notes_list.addItem(item)
        self._add_server_log_files_to_list(query=query)
        if self._search_hits is not None:
            self._append_search_hit_rows_list(self.notes_list)
        self.notes_list.blockSignals(False)

    def _refresh_notes_tree(self, grouped_notes: list[tuple[str, list[NoteDto]]], *, query: str = "") -> None:
        tree = self.notes_tree
        if tree is None:
            return
        # 列表即将重建，先收起悬停浮层，避免残留指向已销毁的 item
        self._hide_folder_hover_popup()

        # ① 清空前先收集所有文件夹的展开状态，合并到持久化字典中
        if not hasattr(self, "_tree_folder_expanded_state"):
            self._tree_folder_expanded_state = {}

        def _is_tracked_folder(role: str) -> bool:
            return (
                role.startswith("__folder__:")
                or role == ASK_AI_ITEM_ID
                or role == SERVER_LOG_FOLDER_ID
                or role.startswith(SERVER_LOG_SUB_PREFIX)
            )

        def _collect_expand_walk(node: QtWidgets.QTreeWidgetItem) -> None:
            role = node.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            if isinstance(role, str) and _is_tracked_folder(role):
                self._tree_folder_expanded_state[role] = node.isExpanded()
            for i in range(node.childCount()):
                _collect_expand_walk(node.child(i))

        try:
            root_collect = tree.invisibleRootItem()
            for i in range(root_collect.childCount()):
                _collect_expand_walk(root_collect.child(i))
        except Exception:
            pass

        tree.blockSignals(True)
        tree.clear()
        ask_ai_item = QtWidgets.QTreeWidgetItem(["▾ 🤖 问AI"])
        ask_ai_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, ASK_AI_ITEM_ID)
        ask_ai_item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, "问AI")
        font = ask_ai_item.font(0)
        font.setBold(True)
        ask_ai_item.setFont(0, font)
        ask_ai_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#B794F4")))
        ask_ai_item.setBackground(0, QtGui.QBrush(QtGui.QColor("rgba(183, 148, 244, 0.06)")))
        tree.addTopLevelItem(ask_ai_item)

        # 收藏的外部/IPC 文件同样全局置顶：并入「★ 收藏」分组
        def _file_matches(file_path: str) -> bool:
            return not query or query.lower() in Path(file_path).name.lower()

        fav_ext = [
            p
            for p in self._external_files
            if self._is_favorite(self._external_key(p)) and _file_matches(p)
        ]
        fav_ipc = [
            p
            for p in self._ipc_files
            if self._is_favorite(self._ipc_key(p)) and _file_matches(p)
        ]
        if (fav_ext or fav_ipc) and not any(
            fn == FAVORITES_GROUP_NAME for fn, _ in (grouped_notes or [])
        ):
            fav_parent = self._ensure_tree_folder_item(tree, FAVORITES_GROUP_NAME)
            if fav_parent is not None:
                self._add_pinned_file_items_tree(fav_parent, fav_ext, fav_ipc)

        if grouped_notes:
            for folder_name, folder_items in grouped_notes:
                if folder_name == FAVORITES_GROUP_NAME:
                    parent = self._ensure_tree_folder_item(tree, FAVORITES_GROUP_NAME)
                    if parent is None:
                        parent = tree.invisibleRootItem()
                    for n in folder_items:
                        parent.addChild(self._create_note_tree_item(n))
                    self._add_pinned_file_items_tree(parent, fav_ext, fav_ipc)
                elif folder_name == "":
                    # 根目录文件放入「未归档」文件夹，不散落在最外层
                    if folder_items:
                        root_parent = self._ensure_tree_folder_item(tree, "未归档")
                        if root_parent is None:
                            root_parent = tree.invisibleRootItem()
                        for n in folder_items:
                            root_parent.addChild(self._create_note_tree_item(n))
                else:
                    parent = self._ensure_tree_folder_item(tree, folder_name)
                    if parent is None:
                        parent = tree.invisibleRootItem()
                    for n in folder_items:
                        parent.addChild(self._create_note_tree_item(n))
        elif query:
            empty_item = QtWidgets.QTreeWidgetItem([f"未找到匹配笔记：{query}"])
            empty_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "__empty__")
            tree.addTopLevelItem(empty_item)
        if self._external_files:
            ext_folder = QtWidgets.QTreeWidgetItem(["📁 外部文件"])
            ext_folder.setData(0, QtCore.Qt.ItemDataRole.UserRole, "__external_folder__")
            ext_font = ext_folder.font(0)
            ext_font.setBold(True)
            ext_folder.setFont(0, ext_font)
            ext_folder.setForeground(0, QtGui.QBrush(QtGui.QColor("#90A4AE")))
            tree.addTopLevelItem(ext_folder)
            for file_path in self._external_files:
                if self._is_favorite(self._external_key(file_path)):
                    continue  # 已置顶到「★ 收藏」
                path = Path(file_path)
                if query and query.lower() not in path.name.lower():
                    continue
                mark, missing = self._file_entry_marker(file_path)
                item = QtWidgets.QTreeWidgetItem([f"  {mark} {path.name}", f"  {file_path}"])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, f"{EXTERNAL_FILE_PREFIX}{file_path}")
                item.setToolTip(0, self._file_entry_tooltip(file_path, missing))
                if missing:
                    item.setForeground(0, QtGui.QBrush(self._missing_entry_color()))
                ext_folder.addChild(item)
        if self._ipc_files:
            ipc_folder = QtWidgets.QTreeWidgetItem(["📁 IPC 文件"])
            ipc_folder.setData(0, QtCore.Qt.ItemDataRole.UserRole, IPC_FILE_FOLDER_ID)
            ipc_font = ipc_folder.font(0)
            ipc_font.setBold(True)
            ipc_folder.setFont(0, ipc_font)
            ipc_folder.setForeground(0, QtGui.QBrush(QtGui.QColor("#90A4AE")))
            tree.addTopLevelItem(ipc_folder)
            for file_path in self._ipc_files:
                if self._is_favorite(self._ipc_key(file_path)):
                    continue  # 已置顶到「★ 收藏」
                path = Path(file_path)
                if query and query.lower() not in path.name.lower():
                    continue
                mark, missing = self._file_entry_marker(file_path)
                item = QtWidgets.QTreeWidgetItem([f"  {mark} {path.name}", f"  {file_path}"])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, f"{IPC_FILE_PREFIX}{file_path}")
                item.setToolTip(0, self._file_entry_tooltip(file_path, missing))
                if missing:
                    item.setForeground(0, QtGui.QBrush(self._missing_entry_color()))
                ipc_folder.addChild(item)
        self._add_server_log_files_to_tree(tree, query=query)
        if self._search_hits is not None:
            self._append_search_hit_rows_tree(tree)
        # ② 按持久化的展开状态恢复（未记录的文件夹默认展开；ASK_AI 默认折叠由外部控制）
        def _restore_expand_walk(node: QtWidgets.QTreeWidgetItem) -> None:
            role = node.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            if isinstance(role, str) and _is_tracked_folder(role):
                expanded = self._tree_folder_expanded_state.get(role, True)
                node.setExpanded(bool(expanded))
                # 同步图标（▾/▸）和子项计数
                self._update_folder_icon(node)
            for i in range(node.childCount()):
                _restore_expand_walk(node.child(i))

        try:
            root_restore = tree.invisibleRootItem()
            for i in range(root_restore.childCount()):
                _restore_expand_walk(root_restore.child(i))
        except Exception:
            tree.expandAll()

        # 刷新所有文件夹项的图标和子项计数
        self._refresh_all_folder_icons()
        tree.blockSignals(False)

    def _refresh_all_folder_icons(self) -> None:
        """遍历树中所有文件夹/ASK_AI 项，更新展开图标和子项计数。"""
        tree = self.notes_tree
        if tree is None:
            return

        def walk(item: QtWidgets.QTreeWidgetItem) -> None:
            role = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            if isinstance(role, str) and (
                role.startswith("__folder__:") or role == ASK_AI_ITEM_ID
                or role == SERVER_LOG_FOLDER_ID
                or role.startswith(SERVER_LOG_SUB_PREFIX)
            ):
                self._update_folder_icon(item)
            for i in range(item.childCount()):
                walk(item.child(i))

        root = tree.invisibleRootItem()
        for i in range(root.childCount()):
            walk(root.child(i))

    def _on_folder_expanded_or_collapsed(self, item: QtWidgets.QTreeWidgetItem) -> None:
        """itemExpanded/itemCollapsed 信号的处理：实时切换 ▾/▸，并持久化展开状态。"""
        self._update_folder_icon(item)
        # 持久化当前文件夹的展开状态，供刷新/重建后恢复
        role = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
        if isinstance(role, str) and (
            role.startswith("__folder__:") or role == ASK_AI_ITEM_ID
            or role == SERVER_LOG_FOLDER_ID
            or role.startswith(SERVER_LOG_SUB_PREFIX)
        ):
            if not hasattr(self, "_tree_folder_expanded_state"):
                self._tree_folder_expanded_state = {}
            self._tree_folder_expanded_state[role] = item.isExpanded()

    def _get_log_server_url(self) -> str:
        """获取日志服务器基础 URL。"""
        api = self.api
        # NotepadApi 有 base_url 属性
        if hasattr(api, "base_url"):
            return api.base_url
        # LocalNotepadApi 有 _log_server_url 静态方法
        if hasattr(api, "_log_server_url"):
            return api._log_server_url()
        return ""

    def _on_notes_tree_context_menu(self, pos: QtCore.QPoint) -> None:
        """notes_tree 右键菜单（弹菜单期间不因选中变化而加载文件）。"""
        try:
            self._on_notes_tree_context_menu_impl(pos)
        finally:
            self._end_right_click_guard()

    def _on_notes_tree_context_menu_impl(self, pos: QtCore.QPoint) -> None:
        """notes_tree 右键菜单"""
        tree = self.notes_tree
        if tree is None:
            return
        item = tree.itemAt(pos)
        if item is None:
            return
        item_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(item_id, str) and item_id.startswith(SERVER_LOG_PREFIX):
            log_path = item_id[len(SERVER_LOG_PREFIX):]
            self._show_log_context_menu(tree.viewport().mapToGlobal(pos), log_path)
            return
        fav_key = _NoteTreeItemDelegate._favorite_key_for_role(item_id)
        if fav_key is not None:
            # 笔记 / 外部文件 / IPC 文件：提供收藏/取消收藏（全局置顶）+ 打开文件所在文件夹
            menu = QtWidgets.QMenu(self)
            is_fav = self._is_favorite(fav_key)
            act_fav = menu.addAction("取消收藏" if is_fav else "收藏并置顶")
            act_fav.triggered.connect(
                lambda _checked=False, k=fav_key: self._toggle_favorite_key(k)
            )
            local_path = self._item_local_file_path(
                item_id, item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
            )
            if local_path is not None:
                menu.addSeparator()
                act_open_dir = menu.addAction("📂 打开文件所在文件夹")
                act_open_dir.triggered.connect(
                    lambda _checked=False, p=local_path: self._open_path_in_explorer(p)
                )
                act_copy_path = menu.addAction("📋 复制文件路径")
                act_copy_path.triggered.connect(
                    lambda _checked=False, p=local_path: self._copy_text_to_clipboard(
                        str(p), label="文件路径"
                    )
                )
                act_copy_dir = menu.addAction("📁 复制所在文件夹")
                act_copy_dir.triggered.connect(
                    lambda _checked=False, p=local_path: self._copy_text_to_clipboard(
                        str(p.parent), label="所在文件夹"
                    )
                )
            menu.exec(tree.viewport().mapToGlobal(pos))

    def _on_notes_list_context_menu(self, pos: QtCore.QPoint) -> None:
        """notes_list 右键菜单（弹菜单期间不因选中变化而加载文件）。"""
        try:
            self._on_notes_list_context_menu_impl(pos)
        finally:
            self._end_right_click_guard()

    def _on_notes_list_context_menu_impl(self, pos: QtCore.QPoint) -> None:
        """notes_list 右键菜单"""
        lst = self.notes_list
        if lst is None:
            return
        item = lst.itemAt(pos)
        if item is None:
            return
        item_id = item.data(QtCore.Qt.ItemDataRole.UserRole) or ""
        if isinstance(item_id, str) and item_id.startswith(SERVER_LOG_PREFIX):
            log_path = item_id[len(SERVER_LOG_PREFIX):]
            self._show_log_context_menu(lst.viewport().mapToGlobal(pos), log_path)
            return
        local_path = self._item_local_file_path(
            item_id, item.data(QtCore.Qt.ItemDataRole.UserRole + 1)
        )
        if local_path is None:
            return
        menu = QtWidgets.QMenu(self)
        act_open_dir = menu.addAction("📂 打开文件所在文件夹")
        act_open_dir.triggered.connect(
            lambda _checked=False, p=local_path: self._open_path_in_explorer(p)
        )
        act_copy_path = menu.addAction("📋 复制文件路径")
        act_copy_path.triggered.connect(
            lambda _checked=False, p=local_path: self._copy_text_to_clipboard(
                str(p), label="文件路径"
            )
        )
        act_copy_dir = menu.addAction("📁 复制所在文件夹")
        act_copy_dir.triggered.connect(
            lambda _checked=False, p=local_path: self._copy_text_to_clipboard(
                str(p.parent), label="所在文件夹"
            )
        )
        menu.exec(lst.viewport().mapToGlobal(pos))

    def _show_log_context_menu(self, global_pos: QtCore.QPoint, log_path: str) -> None:
        """显示服务器日志项的右键菜单"""
        menu = QtWidgets.QMenu(self)

        action_open = menu.addAction("打开日志")
        action_open.triggered.connect(lambda: self._set_server_log_editor(log_path))

        base_url = self._get_log_server_url()
        log_url = f"{base_url}/api/logs/{log_path}" if base_url else ""
        if log_url:
            action_web = menu.addAction("使用网页访问该日志")
            action_web.triggered.connect(lambda: webbrowser.open(log_url))

        menu.addSeparator()

        action_copy_name = menu.addAction("复制文件名")
        action_copy_name.triggered.connect(
            lambda: clipboard_recorder.write_to_clipboard(
                text=PurePosixPath(log_path).name)
        )

        action_copy_path = menu.addAction("复制日志路径")
        action_copy_path.triggered.connect(
            lambda: clipboard_recorder.write_to_clipboard(text=log_path)
        )

        if log_url:
            action_copy_url = menu.addAction("复制网页链接")
            action_copy_url.triggered.connect(
                lambda: clipboard_recorder.write_to_clipboard(text=log_url)
            )

        menu.addSeparator()

        action_download = menu.addAction("下载到本地")
        action_download.triggered.connect(lambda: self._download_server_log(log_path))

        action_delete = menu.addAction("删除日志")
        action_delete.triggered.connect(lambda: self._delete_server_log(log_path))

        menu.addSeparator()

        action_refresh = menu.addAction("刷新日志列表")
        action_refresh.triggered.connect(lambda: self._reload_server_logs())

        menu.exec(global_pos)

    def _download_server_log(self, log_path: str) -> None:
        """下载服务器日志到本地文件。"""
        file_name = PurePosixPath(log_path).name
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "下载日志到本地", file_name
        )
        if not target:
            return
        content = self._get_log_content_cached(log_path)
        if content is None:
            return
        try:
            Path(target).write_text(content, encoding="utf-8")
        except Exception as exc:
            self._show_error(f"下载失败：{exc}")
            return
        self.status.showMessage(f"已下载日志到 {target}", 2500)

    def _delete_server_log(self, log_path: str) -> None:
        """删除服务器日志（带确认）。"""
        file_name = PurePosixPath(log_path).name
        ret = QtWidgets.QMessageBox.question(
            self,
            "删除日志",
            f"确定删除日志 {file_name} 吗？此操作不可恢复。",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.delete_log(log_path)
        except Exception as exc:
            self._show_error(f"删除日志失败：{exc}")
            return
        self._invalidate_log_content_cache(log_path)
        # 若正在编辑该日志，清空编辑器
        if self._current_server_log_path == log_path:
            self._stop_server_log_tail()
            self._current_server_log_path = None
            self._set_editor(None)
        self.status.showMessage(f"已删除日志 {file_name}", 2500)
        self._reload_server_logs()

    def _ensure_tree_folder_item(self, tree: QtWidgets.QTreeWidget, folder_path: str) -> QtWidgets.QTreeWidgetItem:
        parts = [p for p in folder_path.replace("\\", "/").split("/") if p]
        parent = tree.invisibleRootItem()
        current_path = []
        for part in parts:
            current_path.append(part)
            current_path_str = "/".join(current_path)
            found = None
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, QtCore.Qt.ItemDataRole.UserRole) == f"__folder__:{current_path_str}":
                    found = child
                    break
            if found is None:
                # 初始显示展开状态（▾），折叠后通过 _update_folder_icon 切换为 ▸
                found = QtWidgets.QTreeWidgetItem([f"▾ 📁 {part}"])
                found.setData(0, QtCore.Qt.ItemDataRole.UserRole, f"__folder__:{current_path_str}")
                found.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, part)  # 保留纯名称用于更新计数/图标
                found.setSizeHint(0, QtCore.QSize(0, 20))
                folder_font = found.font(0)
                folder_font.setBold(True)
                found.setFont(0, folder_font)
                found.setForeground(0, QtGui.QBrush(QtGui.QColor("#89DDFF")))
                # 文件夹项的背景微亮，区别于子项
                found.setBackground(0, QtGui.QBrush(QtGui.QColor("rgba(137, 221, 255, 0.06)")))
                parent.addChild(found)
                # 默认展开
                found.setExpanded(True)
            parent = found
        return parent

    def _update_folder_icon(self, item: QtWidgets.QTreeWidgetItem) -> None:
        """根据文件夹展开状态更新图标（▾/▸）并刷新子项计数。"""
        if item is None:
            return
        role = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
        if not (isinstance(role, str) and role.startswith("__folder__:")):
            return
        base_name = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or ""
        if not base_name:
            # 回退：从当前文本中提取名称
            cur = (item.text(0) or "").strip()
            # 去除 ▾/▸ 与 📁 前缀
            for prefix in ("▾", "▸"):
                if cur.startswith(prefix):
                    cur = cur[len(prefix):].strip()
            if cur.startswith("📁"):
                cur = cur[2:].strip()
            # 去掉尾部的 " (N)" 计数
            if " (" in cur and cur.endswith(")"):
                cur = cur.rsplit(" (", 1)[0]
            base_name = cur
        # 统计直接子项（仅笔记项，排除子文件夹）
        note_count = 0
        for i in range(item.childCount()):
            child = item.child(i)
            cid = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(cid, int):
                note_count += 1
        icon = "▾" if item.isExpanded() else "▸"
        count_text = f" ({note_count})" if note_count > 0 else ""
        item.setText(0, f"{icon} 📁 {base_name}{count_text}")

    _MAX_FILENAME_CHARS = 30

    @staticmethod
    def _truncate_filename(name: str, limit: int = 30) -> str:
        """文件名超过 limit 个字符时，截断并加 '…'。"""
        if not name or len(name) <= limit:
            return name
        return name[:limit] + "…"

    def _create_note_list_item(self, note: NoteDto) -> QtWidgets.QListWidgetItem:
        display_title = self._truncate_filename(Path(note.title).name)
        # 收藏状态改由右侧星标图标表达，不再使用 ※ 前缀
        item = QtWidgets.QListWidgetItem(f"  {display_title}\n  {note.updated_at}")
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, note.id)
        item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, note.title)
        item.setToolTip(f"{Path(note.title).name}  #{note.id}  {note.updated_at}")
        item.setSizeHint(QtCore.QSize(0, 40))
        item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        return item

    def _create_note_tree_item(self, note: NoteDto) -> QtWidgets.QTreeWidgetItem:
        display_title = self._truncate_filename(Path(note.title).name)
        # 收藏状态改由右侧星标图标表达，不再使用 ※ 前缀
        # 日期简短格式：将 ISO 8601 中的 'T' 替换为空格，并只保留分钟
        updated_at = (note.updated_at or "").replace("T", " ").split(":", 2)
        short_date = ":".join(updated_at[:2]) if len(updated_at) >= 2 else (note.updated_at or "")
        # 文件名 + 日期换行显示
        item = QtWidgets.QTreeWidgetItem([f"{display_title}\n{short_date}"])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        # 文件名字体色，日期用浅灰（通过 foreground 统一色，让换行后的第二行更淡）
        item.setForeground(0, QtGui.QBrush(QtGui.QColor("#E9EEF5")))
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, note.id)
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, note.title)
        item.setToolTip(0, f"{Path(note.title).name}  #{note.id}  {note.updated_at}")
        return item

    def _add_pinned_file_items_tree(self, parent, ext_paths, ipc_paths) -> None:
        """把已收藏的外部/IPC 文件项加入「★ 收藏」分组（全局置顶）。"""
        for file_path in ext_paths:
            mark, missing = self._file_entry_marker(file_path)
            item = QtWidgets.QTreeWidgetItem(
                [f"  {mark} {Path(file_path).name}", f"  {file_path}"]
            )
            item.setToolTip(0, self._file_entry_tooltip(file_path, missing))
            if missing:
                item.setForeground(0, QtGui.QBrush(self._missing_entry_color()))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, self._external_key(file_path))
            parent.addChild(item)
        for file_path in ipc_paths:
            mark, missing = self._file_entry_marker(file_path)
            item = QtWidgets.QTreeWidgetItem(
                [f"  {mark} {Path(file_path).name}", f"  {file_path}"]
            )
            item.setToolTip(0, self._file_entry_tooltip(file_path, missing))
            if missing:
                item.setForeground(0, QtGui.QBrush(self._missing_entry_color()))
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, self._ipc_key(file_path))
            parent.addChild(item)

    def _group_notes_by_folder(
        self,
        notes: list[NoteDto],
        *,
        query: str = "",
    ) -> list[tuple[str, list[NoteDto]]]:
        grouped: dict[str, list[NoteDto]] = {}
        favorites: list[NoteDto] = []
        fav_rank = {
            nid: i for i, nid in enumerate(self._favorite_order) if isinstance(nid, int)
        }
        for note in notes:
            if note.title.startswith("问AI"):
                continue
            if query and query.lower() not in note.title.lower():
                continue
            if self._is_favorite(note.id):
                favorites.append(note)
                continue
            folder_name = self._note_folder_name(note.title)
            grouped.setdefault(folder_name, []).append(note)
        # 收藏项全局置顶：单独成组放在最前，组内按收藏先后（最近收藏在前）
        favorites.sort(key=lambda n: fav_rank.get(int(n.id), 1 << 30))
        groups = sorted(
            grouped.items(),
            key=lambda kv: (kv[0] == "", kv[0].lower()),
        )
        if favorites:
            groups.insert(0, (FAVORITES_GROUP_NAME, favorites))
        return groups

    @staticmethod
    def _note_folder_name(title: str) -> str:
        path = Path(title)
        parent = path.parent
        if str(parent) in {".", ""}:
            return ""
        return str(parent).replace("\\", "/")

    def _select_note_id(self, note_id: int) -> bool:
        if self._notes_tree_mode and self.notes_tree is not None:
            item = self._find_tree_item_by_note_id(note_id)
            if item is not None:
                # 选中前先展开其所在文件夹（含多级父目录），否则折叠状态下看不到选中项
                self._expand_tree_ancestors(item)
                self.notes_tree.setCurrentItem(item)
                self.notes_tree.scrollToItem(item)
                return True
            return False
        for i in range(self.notes_list.count()):
            item = self.notes_list.item(i)
            item_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if item_id == ASK_AI_ITEM_ID:
                continue
            if isinstance(item_id, str) and item_id.startswith(ASK_AI_SESSION_PREFIX):
                continue
            if isinstance(item_id, str) and item_id.startswith(EXTERNAL_FILE_PREFIX):
                continue
            if isinstance(item_id, str) and item_id.startswith(IPC_FILE_PREFIX):
                continue
            if isinstance(item_id, str) and item_id.startswith(IPC_FILE_PREFIX):
                continue
            if isinstance(item_id, str) and item_id.startswith(SERVER_LOG_PREFIX):
                continue
            if isinstance(item_id, str) and item_id.startswith(SEARCH_HIT_PREFIX):
                continue
            if isinstance(item_id, str) and (
                item_id.startswith("__folder__:") or item_id.startswith("__empty__")
                or item_id == SERVER_LOG_FOLDER_ID
                or item_id.startswith(SERVER_LOG_SUB_PREFIX)
                or item_id == "__server_log_error__"
            ):
                continue
            if int(item_id) == int(note_id):
                self.notes_list.setCurrentRow(i)
                return True
        return False

    def _select_external_file(self, file_path: str) -> bool:
        target = f"{EXTERNAL_FILE_PREFIX}{file_path}"
        if self._notes_tree_mode and self.notes_tree is not None:
            item = self._find_tree_item_by_user_role(target)
            if item is not None:
                self.notes_tree.setCurrentItem(item)
                return True
            return False
        for i in range(self.notes_list.count()):
            item = self.notes_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == target:
                self.notes_list.setCurrentRow(i)
                return True
        return False

    def _select_ipc_file(self, file_path: str) -> bool:
        target = f"{IPC_FILE_PREFIX}{file_path}"
        if self._notes_tree_mode and self.notes_tree is not None:
            item = self._find_tree_item_by_user_role(target)
            if item is not None:
                self.notes_tree.setCurrentItem(item)
                return True
            return False
        for i in range(self.notes_list.count()):
            item = self.notes_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == target:
                self.notes_list.setCurrentRow(i)
                return True
        return False

    def _select_ai_session_id(self, session_id: str) -> None:
        target = f"{ASK_AI_SESSION_PREFIX}{session_id}"
        for i in range(self.notes_list.count()):
            item = self.notes_list.item(i)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == target:
                self.notes_list.setCurrentRow(i)
                return

    def _find_tree_item_by_user_role(self, target) -> QtWidgets.QTreeWidgetItem | None:
        if self.notes_tree is None:
            return None
        root = self.notes_tree.invisibleRootItem()
        stack = [root.child(i) for i in range(root.childCount())]
        while stack:
            item = stack.pop(0)
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == target:
                return item
            for i in range(item.childCount()):
                stack.append(item.child(i))
        return None

    def _find_tree_item_by_note_id(self, note_id: int) -> QtWidgets.QTreeWidgetItem | None:
        return self._find_tree_item_by_user_role(note_id)

    def _notes_view(self):
        """当前生效的笔记视图控件（树或列表）。"""
        for name in ("notes_tree", "notes_list"):
            view = getattr(self, name, None)
            if view is not None and self._qt_is_valid(view):
                return view
        return None

    def _begin_right_click_guard(self, view) -> None:
        """右键按下：记下当前选中项，菜单期间不因选中变化切换编辑器内容。"""
        self._right_click_guard = True
        self._right_click_view = view
        try:
            self._right_click_selection = list(view.selectedItems())
        except Exception:
            self._right_click_selection = []

    def _end_right_click_guard(self) -> None:
        """菜单收起：还原原选中项。

        不还原的话，被右键的那项会一直处于「已选中」状态，用户随后左键点它
        不会产生选中变化，文件就再也打不开了。
        """
        view = self._right_click_view
        selection = self._right_click_selection
        self._right_click_guard = False
        self._right_click_view = None
        self._right_click_selection = []
        if view is None or not self._qt_is_valid(view):
            return
        was_blocked = view.blockSignals(True)
        try:
            view.clearSelection()
            for item in selection:
                if item is not None:
                    item.setSelected(True)
        except Exception:
            pass
        finally:
            view.blockSignals(was_blocked)

    def _on_selection_changed(self) -> None:
        if self._right_click_guard:
            # 右键只是要弹菜单，不改动正在编辑的内容
            lprint("右键弹菜单：忽略本次选中变化")
            return
        if self._in_selection_changed:
            return
        self._in_selection_changed = True
        try:
            self._on_selection_changed_inner()
        except Exception:
            self._append_exception_log("选择切换异常")
            raise
        finally:
            self._in_selection_changed = False
            # 选中子文件时，高亮其父级目录链
            self._highlight_parent_folders()

    # 文件夹默认底色缓存（role -> QColor）
    _FOLDER_DEFAULT_BG: dict = {
        "__ask_ai__": QtGui.QColor(183, 148, 244, 15),
    }
    _FOLDER_DEFAULT_BG_DEFAULT = QtGui.QColor(137, 221, 255, 15)
    # 父级高亮色（透明度 0.8）
    _FOLDER_HIGHLIGHT_BG = QtGui.QColor(77, 130, 220, 50)

    def _highlight_parent_folders(self) -> None:
        """清除所有文件夹的高亮，并为选中项的所有祖先文件夹加高亮底色。

        通过 UserRole+99 标记 item，由 _NoteTreeItemDelegate 在 paint 中叠加绘制，
        避免 QSS 中 ::item{background:transparent} 覆盖 setBackground。
        """
        tree = self.notes_tree
        if tree is None or not self._notes_tree_mode:
            return

        highlight_role = _NoteTreeItemDelegate._PARENT_HIGHLIGHT_ROLE

        # 1. 重置所有文件夹的高亮标记和底色
        root = tree.invisibleRootItem()

        def reset_walk(node: QtWidgets.QTreeWidgetItem) -> None:
            role = node.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            if isinstance(role, str) and (
                role.startswith("__folder__:") or role == ASK_AI_ITEM_ID
            ):
                node.setData(0, highlight_role, False)
                default = (
                    self._FOLDER_DEFAULT_BG.get(role)
                    or self._FOLDER_DEFAULT_BG_DEFAULT
                )
                node.setBackground(0, QtGui.QBrush(default))
            for i in range(node.childCount()):
                reset_walk(node.child(i))

        for i in range(root.childCount()):
            reset_walk(root.child(i))

        # 2. 找出所有选中项的祖先文件夹，并标记高亮
        selected = tree.selectedItems()
        if not selected:
            tree.viewport().update()
            return

        highlight_brush = QtGui.QBrush(self._FOLDER_HIGHLIGHT_BG)
        highlighted: set[int] = set()

        for item in selected:
            # 若当前项本身就是文件夹，也需高亮
            role = item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            if isinstance(role, str) and (
                role.startswith("__folder__:") or role == ASK_AI_ITEM_ID
            ):
                if id(item) not in highlighted:
                    item.setData(0, highlight_role, True)
                    item.setBackground(0, highlight_brush)
                    highlighted.add(id(item))
            # 向上遍历父链
            cur = item.parent()
            while cur is not None:
                role = cur.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
                if isinstance(role, str) and (
                    role.startswith("__folder__:") or role == ASK_AI_ITEM_ID
                ):
                    if id(cur) not in highlighted:
                        cur.setData(0, highlight_role, True)
                        cur.setBackground(0, highlight_brush)
                        highlighted.add(id(cur))
                cur = cur.parent()

        tree.viewport().update()

    def _selected_note_items(self) -> list[object]:
        if self._notes_tree_mode and self.notes_tree is not None:
            return self.notes_tree.selectedItems()
        return self.notes_list.selectedItems()

    def _selected_item_id(self):
        items = self._selected_note_items()
        if not items:
            return None
        item = items[0]
        if self._notes_tree_mode and isinstance(item, QtWidgets.QTreeWidgetItem):
            return item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        return item.data(QtCore.Qt.ItemDataRole.UserRole)

    def _on_selection_changed_inner(self) -> None:
        items = self._selected_note_items()
        item_id = self._selected_item_id()
        lprint(f"选择切换: item_id={item_id!r}, selected={len(items)}")
        if item_id == ASK_AI_ITEM_ID:
            if not self._initializing and self.state.current_note_id is not None:
                self._auto_save_note("切换到问AI前", detail_ui=True)
            lprint("切换到 AI 模式")
            self._set_ai_editor(self._current_ai_session_id)
            # AI 会话不再在列表中显示，不需要选择
            return
        # AI 会话不再在列表中，不再处理 ASK_AI_SESSION_PREFIX

        if self.state.dirty:
            lprint("切换到普通笔记/外部文件前触发自动保存")
            self._auto_save_note("切换日志前", detail_ui=True)

        if not items:
            lprint("未选中任何条目，切换到空编辑器")
            self._set_editor(None)
            return

        self._ask_ai_mode = False
        item_id = self._selected_item_id()
        if isinstance(item_id, str) and item_id.startswith(EXTERNAL_FILE_PREFIX):
            lprint(f"切换到外部文件: {item_id[len(EXTERNAL_FILE_PREFIX):]}")
            self._current_ipc_file = None
            self._set_external_file_editor(item_id[len(EXTERNAL_FILE_PREFIX):])
            return
        if isinstance(item_id, str) and item_id.startswith(IPC_FILE_PREFIX):
            lprint(f"切换到 IPC 文件: {item_id[len(IPC_FILE_PREFIX):]}")
            self._current_external_file = None
            self._current_ipc_file = item_id[len(IPC_FILE_PREFIX):]
            self._set_external_file_editor(self._current_ipc_file)
            return
        if isinstance(item_id, str) and item_id.startswith(SERVER_LOG_PREFIX):
            log_path = item_id[len(SERVER_LOG_PREFIX):]
            lprint(f"切换到服务器日志: {log_path}")
            self._set_server_log_editor(log_path)
            return
        if isinstance(item_id, str) and (
            item_id == SERVER_LOG_FOLDER_ID
            or item_id.startswith(SERVER_LOG_SUB_PREFIX)
            or item_id == "__server_log_error__"
        ):
            return
        if isinstance(item_id, str) and item_id.startswith(SEARCH_HIT_PREFIX):
            # 检索命中行（知识库/未映射笔记）：选中不加载笔记，双击时再浏览器打开
            return
        note_id = int(item_id)
        try:
            note = self.api.get_note(note_id)
        except ApiError as e:
            self._show_error(str(e))
            return
        lprint(f"切换到普通笔记: #{note.id} {note.title}")
        self._set_editor(note)
        self._last_open_note_id = note.id
        self._save_settings()
        self._update_favorite_button_label()

    def _new_note(self) -> None:
        if self._ask_ai_mode:
            session = self._new_ai_session(select=True)
            self.refresh_notes()
            self._select_ai_session_id(session.session_id)
            return
        self._ask_ai_mode = False
        if self.state.dirty and not self._confirm_discard():
            return
        title = self._prompt_new_note_title()
        if not title:
            return
        self._set_editor(None)
        self.title_edit.setText(title)
        self.content_edit.clear()
        self.state.current_note_id = None
        self.state.dirty = True
        self._update_title()

    def _prompt_new_note_title(self) -> str:
        existing = self._existing_note_filenames()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("新建日志文件")
        dialog.setMinimumWidth(520)

        layout = QtWidgets.QVBoxLayout(dialog)
        form_layout = QtWidgets.QFormLayout()
        name_edit = QtWidgets.QLineEdit("未命名", dialog)
        path_label = QtWidgets.QLabel(dialog)
        path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setWordWrap(True)
        form_layout.addRow("文件名：", name_edit)
        form_layout.addRow("完整路径：", path_label)
        layout.addLayout(form_layout)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel,
            dialog,
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def update_path_label() -> None:
            raw = name_edit.text().strip() or "未命名"
            filename = self._unique_note_filename(sanitize_title_to_filename(raw), existing)
            path_label.setText(str(self._notepad_list_dir() / filename))

        name_edit.textChanged.connect(update_path_label)
        update_path_label()

        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return ""
        title = name_edit.text().strip() or "未命名"
        return self._unique_note_filename(sanitize_title_to_filename(title), existing)

    def _unique_note_filename(self, filename: str, existing: set[str]) -> str:
        path = Path(filename)
        stem = path.stem or "未命名"
        suffix = path.suffix
        if filename.lower() not in existing:
            return filename
        for i in range(2, 10_000):
            candidate = f"{stem} ({i}){suffix}"
            if candidate.lower() not in existing:
                return candidate
        raise RuntimeError("cannot find unique filename")

    def _existing_note_filenames(self) -> set[str]:
        try:
            notes = self.api.list_notes()
        except ApiError as e:
            self._show_error(str(e))
            return set()
        return {Path(note.title).name.lower() for note in notes}

    def _save_note(self) -> None:
        if self._ask_ai_mode:
            self._ask_ai()
            return
        if self._current_external_file:
            self._save_external_file()
            return
        if self._current_server_log_path:
            self._save_server_log()
            return
        title = self.title_edit.text().strip() or "未命名"
        content = self._get_content_text()
        old_note_id = self.state.current_note_id
        try:
            if old_note_id is None:
                note = self.api.create_note(title=title, content=content)
            else:
                note = self.api.update_note(old_note_id, title=title, content=content)
        except ApiError as e:
            self._show_error(str(e))
            return

        # 改名/移动会改变本地笔记 id（crc32(相对路径)），需把旧 id 的版本历史迁移到新 id，
        # 否则改一次名字就再也看不到之前保存的历史版本。
        if old_note_id is not None and int(old_note_id) != int(note.id):
            history_store.migrate_versions("note", str(old_note_id), str(note.id))
        self.state.current_note_id = note.id
        self.state.dirty = False
        self.status.showMessage(f"已保存：#{note.id}", 2500)
        self._record_version("note", str(note.id), note.title, content)
        # 记录保存后的快照：本程序自己的写入不应触发「磁盘被外部修改」提示
        self._record_loaded_local_file(self._note_file_path(note.title))
        self.refresh_notes()
        self._update_title()

    # ===== 版本历史 =====

    def _current_version_context(self) -> tuple[str | None, str | None, str]:
        """返回当前编辑内容的版本上下文 (kind, ref, title)。

        「普通笔记」「服务器日志」「本地/外部文件」均支持版本历史，其余返回 (None, None, "")。
        """
        if self._ask_ai_mode:
            return None, None, ""
        if self._current_external_file:
            # 外部文件（含 IPC 推入的本地文件）以绝对路径作为稳定 ref
            return "external", self._current_external_file, Path(self._current_external_file).name
        if self._current_server_log_path:
            log_path = self._current_server_log_path
            return "log", log_path, PurePosixPath(log_path).name
        if self.state.current_note_id is not None:
            return "note", str(self.state.current_note_id), self.title_edit.text().strip()
        return None, None, ""

    def _record_version(self, kind: str, ref: str, title: str, content: str) -> None:
        """保存成功后记录一个历史版本（内容无变化时自动跳过）。"""
        try:
            history_store.add_version(kind, ref, title, content)
        except Exception as exc:
            lprint(f"[l_notepad] WARN: 记录版本历史失败: {exc}")

    def _populate_version_combo(self, *, reset_diff: bool = False) -> None:
        """展开版本下拉框前，重新拉取当前内容的历史版本列表。"""
        combo = getattr(self, "combo_version", None)
        if not self._qt_is_valid(combo):
            return
        combo.blockSignals(True)
        combo.clear()
        kind, ref, _title = self._current_version_context()
        if not kind or not ref:
            combo.addItem("当前内容不支持版本历史", None)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._populate_diff_combos(None, reset=reset_diff)
            return
        # 切换到不同内容时，清除上次选中的版本记录
        ref_key = f"{kind}:{ref}"
        if getattr(self, "_version_combo_ref", None) != ref_key:
            self._version_combo_ref = ref_key
            self._current_version_id = None
        try:
            versions = history_store.list_versions(kind, ref)
        except Exception as exc:
            combo.addItem(f"读取版本历史失败：{exc}", None)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._populate_diff_combos(None, reset=reset_diff)
            return
        if not versions:
            combo.addItem("暂无历史版本", None)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._populate_diff_combos(None, reset=reset_diff)
            return
        total = len(versions)
        for i, v in enumerate(versions):
            num = total - i  # 最早=v1，最新=vN
            label = f"v{num} · {v['saved_at']} · {v['length']}字"
            if i == 0:
                label += "（最新）"
            if v["preview"]:
                label += f" | {v['preview']}"
            combo.addItem(label, v["id"])
            combo.setItemData(combo.count() - 1, label, QtCore.Qt.ItemDataRole.ToolTipRole)
        # 选中当前已应用的版本，使下拉框显示「第几版」
        target_index = 0
        current_id = getattr(self, "_current_version_id", None)
        if current_id is not None:
            for idx in range(combo.count()):
                if combo.itemData(idx) == current_id:
                    target_index = idx
                    break
        combo.setCurrentIndex(target_index)
        combo.blockSignals(False)
        self._populate_diff_combos(versions, reset=reset_diff)

    def _populate_diff_combos(
        self, versions: list[dict] | None, *, reset: bool = False
    ) -> None:
        """填充「对比」两个下拉框。

        默认 = 最新 vs 上一版；切换内容（reset=True）时强制回到默认，否则尽量保留
        用户当前选择（版本被修剪后自动回落默认）。不足两版时禁用对比按钮。
        """
        left = getattr(self, "combo_diff_left", None)
        right = getattr(self, "combo_diff_right", None)
        button = getattr(self, "btn_diff", None)
        if not (self._qt_is_valid(left) and self._qt_is_valid(right)):
            return
        prev_left = left.currentData() if left.count() else None
        prev_right = right.currentData() if right.count() else None
        self._set_diff_enabled(False)
        for combo in (left, right):
            combo.blockSignals(True)
            combo.clear()
        items = list(reversed(versions or []))  # 旧 → 新
        if len(items) < 2:
            left.addItem("（无上一版）", None)
            right.addItem("（无历史版本）", None)
            for combo in (left, right):
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
            return
        ids: list[int] = []
        for i, v in enumerate(items):
            label = f"v{i + 1} · {v['saved_at']} · {v['length']}字"
            ids.append(v["id"])
            for combo in (left, right):
                combo.addItem(label, v["id"])
                combo.setItemData(
                    combo.count() - 1, label, QtCore.Qt.ItemDataRole.ToolTipRole
                )
        if not reset and prev_left in ids and prev_right in ids:
            left.setCurrentIndex(ids.index(prev_left))
            right.setCurrentIndex(ids.index(prev_right))
        else:
            left.setCurrentIndex(len(items) - 2)  # 上一版
            right.setCurrentIndex(len(items) - 1)  # 最新
        for combo in (left, right):
            combo.blockSignals(False)
        self._set_diff_enabled(True)
        if self._qt_is_valid(button):
            button.setToolTip("并排对比两个版本的内容")

    def _set_diff_enabled(self, enabled: bool) -> None:
        """对比下拉框/按钮的可用性（无版本、只有一版、问AI 面板时禁用）。"""
        for name in ("combo_diff_left", "combo_diff_right"):
            widget = getattr(self, name, None)
            if self._qt_is_valid(widget):
                widget.setEnabled(enabled)
        button = getattr(self, "btn_diff", None)
        if self._qt_is_valid(button):
            button.setEnabled(enabled)
            if not enabled:
                button.setToolTip("至少需要两个版本才能对比")

    def _on_version_combo_activated(self, index: int) -> None:
        """选中某个历史版本后载入其内容。"""
        combo = getattr(self, "combo_version", None)
        if not self._qt_is_valid(combo):
            return
        version_id = combo.itemData(index)
        if version_id is None:
            return
        self._current_version_id = int(version_id)
        self._apply_version(int(version_id))
        # 保持当前选中项，使下拉框显示「第几版」

    def _sync_version_combo_on_open(self) -> None:
        """切换笔记/日志/其它内容时刷新版本下拉框，并默认选中最新版本。"""
        combo = getattr(self, "combo_version", None)
        if not self._qt_is_valid(combo):
            return
        kind, ref, _title = self._current_version_context()
        ref_key = f"{kind}:{ref}" if (kind and ref) else None
        self._version_combo_ref = ref_key
        # 刚载入的内容即该内容的最新版本
        self._current_version_id = None
        if kind and ref:
            try:
                versions = history_store.list_versions(kind, ref)
                if versions:
                    self._current_version_id = versions[0]["id"]
            except Exception:
                pass
        self._populate_version_combo(reset_diff=True)

    def _apply_version(self, version_id: int) -> None:
        """把指定版本的内容载入编辑器（标记为已修改，需手动保存以生效）。"""
        version = history_store.get_version(version_id)
        if version is None:
            self.status.showMessage("该版本不存在", 3000)
            return
        self.content_edit.setPlainText(version["content"])
        self.state.dirty = True
        self._update_title()
        self.status.showMessage(
            f"已载入版本 {version['saved_at']}，保存后生效", 3000
        )

    def _on_diff_clicked(self) -> None:
        """并排对比两个选中的历史版本（默认最新 vs 上一版）。"""
        left_combo = getattr(self, "combo_diff_left", None)
        right_combo = getattr(self, "combo_diff_right", None)
        if not (self._qt_is_valid(left_combo) and self._qt_is_valid(right_combo)):
            return
        left_id = left_combo.itemData(left_combo.currentIndex())
        right_id = right_combo.itemData(right_combo.currentIndex())
        if not left_id or not right_id or left_id == right_id:
            self.status.showMessage("请选择两个不同的版本再对比", 3000)
            return
        left_ver = history_store.get_version(int(left_id))
        right_ver = history_store.get_version(int(right_id))
        if left_ver is None or right_ver is None:
            self.status.showMessage("所选版本已不存在，已刷新版本列表", 4000)
            self._populate_version_combo(reset_diff=True)
            return
        name = left_ver["title"] or right_ver["title"] or "当前内容"
        # 版本列表传给对话框：窗口内可直接切换两侧版本重新对比
        versions = [
            (left_combo.itemData(i), left_combo.itemText(i))
            for i in range(left_combo.count())
            if left_combo.itemData(i) is not None
        ]

        def _load_version(version_id):
            ver = history_store.get_version(int(version_id))
            return None if ver is None else ver["content"]

        dlg = VersionDiffDialog(
            name=name,
            left_label=left_combo.currentText(),
            right_label=right_combo.currentText(),
            left_text=left_ver["content"],
            right_text=right_ver["content"],
            mode=self._diff_mode_from_filename(name),
            dirty_hint=bool(getattr(self.state, "dirty", False)),
            versions=versions,
            version_loader=_load_version,
            left_version_id=int(left_id),
            right_version_id=int(right_id),
            parent=self,
        )
        dlg.exec()

    @staticmethod
    def _format_file_size(num_bytes: int) -> str:
        if num_bytes < 1024:
            return f"{num_bytes} B"
        if num_bytes < 1024 * 1024:
            return f"{num_bytes / 1024:.1f} KB"
        return f"{num_bytes / (1024 * 1024):.1f} MB"

    def _set_code_editor_status_file(self, path: Path) -> None:
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            return
        if hasattr(self.content_edit, "set_source_file"):
            self.content_edit.set_source_file(path)

    def _set_code_editor_status_text(self, display: str, size_bytes: int | None = None) -> None:
        if size_bytes is None:
            text = display
        else:
            text = f"{display}    ({self._format_file_size(size_bytes)})"
        status_bar = self._right_widget_refs.get("CodeEditorStatusBar")
        if self._qt_is_valid(status_bar):
            status_bar.setText(text)
            status_bar.setToolTip(display)

    def _on_preview_load_progress(self, stage: str, done: int, total: int) -> None:
        """Markdown 预览后台加载进度 → 底部状态栏。"""
        phase = "渲染" if stage == "render" else "插入"
        if total > 0:
            percent = int(done * 100 / total)
            text = f"预览加载中（{phase} {done}/{total} · {percent}%）"
        else:
            text = f"预览加载中（{phase}…）"
        status_bar = self._right_widget_refs.get("CodeEditorStatusBar")
        if self._qt_is_valid(status_bar):
            status_bar.setText(text)

    def _on_preview_load_finished(self) -> None:
        """后台加载结束：底部状态栏恢复成文件信息。"""
        path = getattr(self.content_edit, "_current_file_path", None)
        if path:
            try:
                file_path = Path(str(path))
                if file_path.is_file():
                    self._set_code_editor_status_text(
                        str(file_path), file_path.stat().st_size
                    )
                    return
            except OSError:
                pass
        self._set_code_editor_status_text("未加载文件")

    def _on_preview_load_failed(self, message: str) -> None:
        self.status.showMessage(f"预览加载失败：{message}", 5000)

    def _set_ai_status_bar(self, session: AiSession | None) -> None:
        if session is None:
            self._set_code_editor_status_text("AI 会话: 未选择")
            return
        storage = "QSettings: ai/sessions"
        detail = (
            f"AI 会话: {session.title} | session_id={session.session_id} | "
            f"消息={len(session.messages)} | 草稿={len(session.draft_prompt)}字 | 保存位置={storage}"
        )
        self._set_code_editor_status_text(detail)

    def _notepad_list_dir(self) -> Path:
        return paths.notes_dir()

    def _resolve_saved_path(self, title: str, external_path: str | None = None) -> Path | None:
        if external_path:
            path = Path(external_path)
            return path if path.is_file() else None
        matches = list(self._notepad_list_dir().rglob(title))
        if len(matches) == 1:
            return matches[0]
        return None

    def _stat_from_path_or_fallback(
        self,
        path: Path | None,
        *,
        content: str = "",
        updated_at: str | None = None,
    ) -> tuple[int | None, str | None]:
        if path is not None and path.is_file():
            try:
                st = path.stat()
                saved_at = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                return int(st.st_size), saved_at
            except OSError:
                pass
        size_bytes = len(content.encode("utf-8")) if content else None
        saved_at = None
        if updated_at:
            saved_at = str(updated_at)[:19].replace("T", " ")
        return size_bytes, saved_at

    def set_tray_icon(self, tray: QtWidgets.QSystemTrayIcon | None) -> None:
        self._tray_icon = tray

    def _notify_tray(
        self,
        title: str,
        message: str,
        *,
        icon: QtWidgets.QSystemTrayIcon.MessageIcon = QtWidgets.QSystemTrayIcon.MessageIcon.Information,
        timeout_ms: int = 2500,
    ) -> None:
        if self._tray_icon is None:
            return
        self._tray_icon.showMessage(title, message, icon, timeout_ms)

    def _report_autosave(
        self,
        reason: str,
        *,
        ok: bool,
        filename: str,
        size_bytes: int | None = None,
        saved_at: str | None = None,
        error: str | None = None,
        notify_tray: bool = False,
        detail_ui: bool = False,
    ) -> None:
        size_text = self._format_file_size(size_bytes) if size_bytes is not None else "-"
        date_text = saved_at or "-"

        if detail_ui:
            if ok:
                msg = f"{reason} 自动保存：{filename} | 大小 {size_text} | {date_text}"
                timeout = 3500
            else:
                err = error or "未知错误"
                msg = f"{reason} 自动保存失败：{filename} | 大小 {size_text} | {date_text} | {err}"
                timeout = 5000
            self.status.showMessage(msg, timeout)
            lprint(msg)

        if notify_tray:
            if ok:
                self._notify_tray("L Notepad", f"已自动保存：{filename}（{size_text}）")
            else:
                err = error or "未知错误"
                self._notify_tray(
                    "L Notepad",
                    f"自动保存失败：{err}",
                    icon=QtWidgets.QSystemTrayIcon.MessageIcon.Warning,
                    timeout_ms=5000,
                )

    def _auto_save_note(
        self,
        reason: str,
        *,
        notify_tray: bool = False,
        detail_ui: bool = False,
    ) -> None:
        if self._ask_ai_mode or not self.state.dirty:
            return
        if self._current_external_file:
            self._save_external_file(reason, notify_tray=notify_tray, detail_ui=detail_ui)
            return
        if self._current_server_log_path:
            self._save_server_log(reason)
            return
        if self.state.current_note_id is None:
            return
        if not self._qt_is_valid(getattr(self, "title_edit", None)):
            lprint(f"{reason} 自动保存跳过：title_edit 已失效")
            return
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            lprint(f"{reason} 自动保存跳过：content_edit 已失效")
            return
        title = self.title_edit.text().strip() or "未命名"
        content = self._get_content_text()
        old_note_id = self.state.current_note_id
        try:
            note = self.api.update_note(old_note_id, title=title, content=content)
        except ApiError as e:
            if detail_ui or notify_tray:
                self._report_autosave(
                    reason,
                    ok=False,
                    filename=title,
                    error=str(e),
                    notify_tray=notify_tray,
                    detail_ui=detail_ui,
                )
            else:
                self.status.showMessage(f"自动保存失败：{e}", 5000)
                lprint(f"{reason} 自动保存失败：{e}")
            return

        # 自动保存同样要记录版本历史，否则只有在手动 Ctrl+S 时才能留下历史；
        # 改名/移动导致 id 变化时，先把旧 id 的历史迁移到新 id。
        if int(old_note_id) != int(note.id):
            history_store.migrate_versions("note", str(old_note_id), str(note.id))
        self._record_version("note", str(note.id), note.title, content)
        # 记录保存后的快照：本程序自己的写入不应触发「磁盘被外部修改」提示
        self._record_loaded_local_file(self._note_file_path(note.title))
        self.state.current_note_id = note.id
        self._last_open_note_id = note.id
        self.state.dirty = False
        self.refresh_notes()
        self._select_note_id(note.id)
        self._update_title()
        self._save_settings()

        saved_path = self._resolve_saved_path(note.title)
        size_bytes, saved_at = self._stat_from_path_or_fallback(
            saved_path,
            content=content,
            updated_at=note.updated_at,
        )
        if detail_ui or notify_tray:
            self._report_autosave(
                reason,
                ok=True,
                filename=note.title,
                size_bytes=size_bytes,
                saved_at=saved_at,
                notify_tray=notify_tray,
                detail_ui=detail_ui,
            )
        else:
            self.status.showMessage(f"{reason} 已自动保存：#{note.id}", 2500)
            lprint(f"{reason} 自动保存日志文件：#{note.id}")

    def _delete_note(self) -> None:
        if self._ask_ai_mode:
            return
        if self._current_external_file:
            file_path = self._current_external_file
            ret = QtWidgets.QMessageBox.question(self, "移除外部文件", f"从列表移除外部文件？\n{file_path}")
            if ret != QtWidgets.QMessageBox.StandardButton.Yes:
                return
            self._external_files = [x for x in self._external_files if x != file_path]
            self._current_external_file = None
            self.state.current_note_id = None
            self.state.dirty = False
            self._save_external_files_state()
            self.refresh_notes()
            self.status.showMessage("已从列表移除，硬盘文件未删除", 2500)
            self._update_title()
            return
        if self.state.current_note_id is None:
            return
        note_id = self.state.current_note_id
        ret = QtWidgets.QMessageBox.question(self, "删除笔记", f"确定删除笔记 #{note_id}？")
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.api.delete_note(note_id)
        except ApiError as e:
            self._show_error(str(e))
            return
        self.state.current_note_id = None
        self.state.dirty = False
        self.refresh_notes()
        self.status.showMessage("已删除", 2500)
        self._update_title()

    def _external_files_state_path(self) -> Path:
        return paths.external_files_state_file()

    def _load_external_files_state(self) -> None:
        state_path = self._external_files_state_path()
        self._external_files = []
        self._current_external_file = None
        if not state_path.exists():
            return
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        files = data.get("files", []) if isinstance(data, dict) else []
        current = data.get("current", "") if isinstance(data, dict) else ""
        if isinstance(files, list):
            seen: set[str] = set()
            for item in files:
                file_path = str(item).strip()
                if file_path and file_path not in seen:
                    seen.add(file_path)
                    self._external_files.append(file_path)
        current_path = str(current).strip()
        if current_path in self._external_files:
            self._current_external_file = current_path

    def _save_external_files_state(self) -> None:
        data = {
            "files": self._external_files,
            "current": self._current_external_file or "",
        }
        self._external_files_state_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_ipc_file(self, file_path: str) -> None:
        file_path = str(Path(file_path))
        if file_path not in self._ipc_files:
            self._ipc_files.insert(0, file_path)
        self._current_ipc_file = file_path
        self._current_external_file = None
        self._save_external_files_state()
        self.refresh_notes()
        self._select_ipc_file(file_path)

    def _open_external_file(self) -> None:
        if self.state.dirty and not self._confirm_discard():
            return
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "打开硬盘文件", "")
        if not file_path:
            return
        file_path = str(Path(file_path))
        if file_path not in self._external_files:
            self._external_files.insert(0, file_path)
        self._current_external_file = file_path
        self.state.current_note_id = None
        self.state.dirty = False
        self._save_external_files_state()
        self.refresh_notes()
        self._select_external_file(file_path)

    def _qt_is_valid(self, widget) -> bool:
        """检查 PySide 包装对象背后的 C++ 对象是否仍然有效。"""
        if widget is None:
            return False
        if shiboken6 is None:
            return True
        try:
            return bool(shiboken6.isValid(widget))
        except Exception:
            return False

    def _remember_right_widget(self, key: str, widget: QtWidgets.QWidget | None) -> None:
        if widget is None:
            return
        self._right_widget_refs[key] = widget

    def _get_right_widget(self, key: str, fallback_attr: str | None = None, expected_type=None):
        widget = self._right_widget_refs.get(key)
        if widget is None and fallback_attr is not None:
            widget = getattr(self, fallback_attr, None)
        if expected_type is not None and not isinstance(widget, expected_type):
            widget = getattr(self, key, None)
        if self._qt_is_valid(widget):
            return widget
        if fallback_attr:
            widget = getattr(self, fallback_attr, None)
            if self._qt_is_valid(widget):
                self._right_widget_refs[key] = widget
                return widget
        return None

    def _refresh_core_widget_refs(self) -> None:
        """右侧面板失效时，通过重建 RightPanel 实例刷新全部右侧控件引用。

        只有当关键右侧控件失效时才重建，避免每次调用都丢失编辑状态。
        """
        critical = ("title_edit", "ai_widget", "log_widget", "content_edit", "ai_tabs", "btn_save", "ai_answer_edit")
        need_rebuild = False
        for key in critical:
            widget = self._right_widget_refs.get(key) or getattr(self, key, None)
            if not self._qt_is_valid(widget):
                need_rebuild = True
                break
        if not need_rebuild:
            return
        self._rebuild_right_panel()

    def _rebuild_right_panel(self) -> None:
        """销毁旧的右侧面板并新建 RightPanel 实例。"""
        splitter = self.findChild(QtWidgets.QSplitter, "splitter_main")
        if splitter is None:
            lprint("警告: 重建右侧面板时找不到 splitter_main，跳过")
            return

        # 移除当前右侧面板（含旧 RightPanel 及其全部子控件）
        for _i in range(splitter.count()):
            _w = splitter.widget(_i)
            if _w is not None and _w.objectName() == "right_panel":
                _w.setParent(None)
                _w.deleteLater()
                break

        self._right_panel = RightPanel(self)
        splitter.addWidget(self._right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self._right_panel.bind_to(self)
        self._connect_right_panel_signals()

    def _connect_right_panel_signals(self) -> None:
        """把重建后的右侧面板控件与 MainWindow 信号连接起来。"""
        # AI 标签页
        self.ai_tabs.tabCloseRequested.connect(self._on_ai_tab_close_requested)
        self.ai_tabs.currentChanged.connect(self._on_ai_tab_changed)
        self.ai_tabs.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.ai_tabs.customContextMenuRequested.connect(self._on_ai_tabs_context_menu)
        self._setup_ai_tab_close_buttons()

        # 标题编辑
        self.title_edit.textEdited.connect(self._mark_dirty)

        # 提供商选择
        self.provider_combo.clear()
        for provider_name in AI_PROVIDERS:
            self.provider_combo.addItem(provider_name)
        saved_provider = self._settings.value("ai/provider", DEFAULT_PROVIDER)
        if saved_provider in AI_PROVIDERS:
            self.provider_combo.setCurrentText(saved_provider)
        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)

        # 模型列表
        self._update_model_list_for_provider(self.provider_combo.currentText())
        self.model_combo.setCurrentText(self._selected_ai_model)
        self.model_combo.currentTextChanged.connect(self._on_ai_model_changed)
        self.btn_refresh_models.clicked.connect(self._refresh_ai_models)

        # 笔记区编辑器
        self.content_edit.editor().textChanged.connect(self._mark_dirty)
        self.content_edit.editor().textChanged.connect(self._update_realtime_token_stats)


        self.btn_save.setObjectName("PrimaryButton")
        self.btn_ai_ask.setObjectName("PrimaryButton")
        self.btn_delete.setObjectName("DangerButton")

        # 按钮
        self.btn_new.clicked.connect(self._new_note)
        self.btn_save.clicked.connect(self._save_note)
        self.btn_delete.clicked.connect(self._delete_note)
        self.btn_refresh.clicked.connect(self._on_refresh_button_clicked)
        self.btn_favorite.clicked.connect(self._toggle_favorite_current)
        self.btn_ai_ask.clicked.connect(self._ask_ai)
        if self._qt_is_valid(getattr(self, "btn_ai_clear", None)):
            self.btn_ai_clear.clicked.connect(self._clear_ai_context)

        # Markdown 预览后台加载：底部状态栏显示进度，结束/失败后恢复
        self.content_edit.preview_load_progress().connect(self._on_preview_load_progress)
        self.content_edit.preview_load_finished().connect(self._on_preview_load_finished)
        self.content_edit.preview_load_failed().connect(self._on_preview_load_failed)
        self.combo_version.aboutToShowPopup.connect(self._populate_version_combo)
        self.combo_version.activated.connect(self._on_version_combo_activated)
        self.btn_diff.clicked.connect(self._on_diff_clicked)

        # 编辑器事件过滤
        for editor_widget in (self.content_edit, self.ai_answer_edit):
            if hasattr(editor_widget, "editor"):
                editor = editor_widget.editor()
                editor.installEventFilter(self)
                editor.viewport().installEventFilter(self)

        # 初始隐藏 AI 相关控件
        for w in (self.provider_combo, self.label_model, self.model_combo,
                  self.btn_refresh_models, self.label_input_tokens,
                  self.label_output_tokens, self.label_cost, self.label_price_source):
            if w is not None:
                w.hide()


    def _ensure_main_tab_active(self, reason: str) -> None:
        """切换左侧笔记/AI 时确保主界面页是当前页，否则右侧父级不可见。"""
        self._refresh_core_widget_refs()
        if not self._qt_is_valid(getattr(self, "tabs", None)) or not self._qt_is_valid(getattr(self, "_tab_main_widget", None)):
            return
        main_index = self.tabs.indexOf(self._tab_main_widget)
        if main_index < 0:
            return
        if self.tabs.currentIndex() != main_index:
            self.tabs.setCurrentIndex(main_index)
            lprint(f"已切回主界面页: reason={reason}, index={main_index}")

    def _set_right_panel_mode(self, mode: str) -> None:
        """统一切换右侧主编辑区域，避免 AI/普通笔记/外部文件状态残留。"""
        self._refresh_core_widget_refs()
        self._ensure_main_tab_active(f"right_panel:{mode}")
        is_ai = mode == "ai"
        is_editor = mode in {"note", "external", "empty"}
        ai_tabs_valid = self._qt_is_valid(getattr(self, "ai_tabs", None))
        ai_widget_valid = self._qt_is_valid(getattr(self, "ai_widget", None))
        log_widget_valid = self._qt_is_valid(getattr(self, "log_widget", None))
        content_valid = self._qt_is_valid(getattr(self, "content_edit", None))
        answer_valid = self._qt_is_valid(getattr(self, "ai_answer_edit", None))
        if ai_widget_valid:
            self.ai_widget.setVisible(is_ai)
        elif is_ai:
            lprint("警告: ai_widget 已失效，无法显示 AI 面板")
        if log_widget_valid:
            self.log_widget.setVisible(is_editor)
        elif is_editor:
            lprint("警告: log_widget 已失效，无法显示日志面板")
        if ai_tabs_valid:
            self.ai_tabs.setVisible(True)
        elif is_ai:
            lprint("警告: ai_tabs 已失效，无法显示 AI 面板")
        if content_valid:
            self.content_edit.setVisible(True)
        else:
            lprint("警告: content_edit 已失效，无法显示普通笔记面板")
        if answer_valid:
            self.ai_answer_edit.hide()
        if is_ai and ai_widget_valid:
            self.ai_widget.raise_()
        elif is_editor and log_widget_valid:
            self.log_widget.raise_()
            if content_valid:
                self._raise_content_editor_surface()
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        parent = self.content_edit.parentWidget() if content_valid else None
        lprint(
            "右侧面板切换: "
            f"mode={mode}, current_tab={self.tabs.currentIndex() if self._qt_is_valid(getattr(self, 'tabs', None)) else -1}, "
            f"ai_widget_valid={ai_widget_valid}, "
            f"ai_widget={self.ai_widget.isVisible() if ai_widget_valid else None}, "
            f"log_widget_valid={log_widget_valid}, "
            f"log_widget={self.log_widget.isVisible() if log_widget_valid else None}, "
            f"ai_tabs_valid={ai_tabs_valid}, "
            f"ai_tabs={self.ai_tabs.isVisible() if ai_tabs_valid else None}, "
            f"content_valid={content_valid}, "
            f"content_edit={self.content_edit.isVisible() if content_valid else None}, "
            f"content_hidden={self.content_edit.isHidden() if content_valid else None}, "
            f"content_parent_visible={parent.isVisible() if parent else None}, "
            f"content_size={self.content_edit.size().width()}x{self.content_edit.size().height() if content_valid else -1}, "
            f"ai_answer_edit={self.ai_answer_edit.isVisible() if answer_valid else None}, "
            f"ai_tabs_count={self.ai_tabs.count() if ai_tabs_valid else None}"
        )

    def _raise_content_editor_surface(self) -> None:
        """抬起右侧内容编辑区域，同时保留 Markdown 预览层在最上方。"""
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            return
        self.content_edit.raise_()
        try:
            if self.content_edit.is_markdown_preview_mode():
                editor = self.content_edit.editor()
                preview_view = getattr(editor, "_preview_view", None)
                if preview_view is not None and self._qt_is_valid(preview_view):
                    if hasattr(editor, "_sync_markdown_preview_geometry"):
                        editor._sync_markdown_preview_geometry()
                    preview_view.show()
                    preview_view.raise_()
        except Exception as exc:
            lprint(f"Markdown 预览层置顶跳过: {exc!r}")

    def _set_ai_controls_visible(self, visible: bool) -> None:
        """统一切换 AI 顶部/状态栏相关控件可见性。"""
        widgets = (
            self.title_edit,
            self.tab_count,
            self.provider_combo,
            self.label_model,
            self.model_combo,
            self.btn_refresh_models,
            self.label_input_tokens,
            self.label_output_tokens,
            self.label_cost,
            self.label_price_source,
        )
        changed = 0
        skipped: list[str] = []
        for widget in widgets:
            if not self._qt_is_valid(widget):
                if widget is not None:
                    try:
                        skipped.append(widget.objectName() or widget.__class__.__name__)
                    except Exception:
                        skipped.append(type(widget).__name__)
                continue
            widget.setVisible(visible)
            changed += 1
        # 切换版本按钮：仅非问AI面板显示
        # 版本下拉框与标签：仅非问AI面板显示
        for name in (
            "combo_version",
            "label_version",
            "label_diff",
            "combo_diff_left",
            "label_diff_arrow",
            "combo_diff_right",
            "btn_diff",
        ):
            widget = getattr(self, name, None)
            if self._qt_is_valid(widget):
                widget.setVisible(not visible)
        if skipped:
            lprint(f"AI 控件可见性跳过已失效控件: {', '.join(skipped)}")
        lprint(f"AI 控件可见性: {visible}, changed={changed}")

    def _set_external_file_editor(self, file_path: str) -> None:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            # 外部文件不存在：温和提示，不弹模态错误框（避免被误认为崩溃）
            lprint(f"外部文件不存在：{file_path}")
            self.status.showMessage(f"外部文件不存在：{file_path}", 5000)
            return
        mode = self._mode_from_filename(path.name)
        self._ask_ai_mode = False
        self._current_ai_session_id = None
        self._current_external_file = str(path)
        self._current_server_log_path = None
        self.state.current_note_id = None
        if not self.content_edit.load_text_file_cached(path, mode=mode):
            self._show_error(f"打开外部文件失败：{file_path}")
            self._current_external_file = None
            return
        self.title_edit.blockSignals(True)
        self.content_edit.blockSignals(True)
        self.title_edit.setText(path.name)
        self.title_edit.blockSignals(False)
        self.content_edit.blockSignals(False)
        self._set_right_panel_mode("external")
        self._set_code_editor_status_file(path)
        content = self.content_edit.toPlainText()
        self._set_code_editor_status_text(str(path), len(content.encode("utf-8")))
        self._set_ai_controls_visible(False)
        self.btn_ai_ask.setEnabled(False)
        self.btn_save.setEnabled(True)
        self.btn_delete.setEnabled(True)
        self.btn_favorite.setEnabled(True)
        self._auto_set_highlight_mode(path.name)
        self.state.dirty = False
        self._record_loaded_local_file(path)
        # 打开时先落一份基线版本（内容与上一版本相同会被 add_version 去重），
        # 这样首次编辑后的原内容仍可回退，而不会只剩编辑后的新内容。
        self._record_version("external", self._current_external_file, path.name, content)
        self._save_external_files_state()
        self._update_title()
        self._log_file_path_label.setText(f"日志路径: {path}")
        self._log_file_path_label.setToolTip(str(path))
        self._sync_version_combo_on_open()
        self._update_favorite_button_label()

    # ===== 服务器日志文件浏览（通过 API 获取） =====

    def _fetch_server_logs(self) -> tuple[list[LogDto], str | None]:
        """通过 API 获取服务器日志列表。返回 (logs, error_msg)。
        
        注意：此方法可在后台线程中调用，不应直接操作 UI。
        """
        try:
            return self.api.list_logs(), None
        except Exception as e:
            msg = f"获取服务器日志列表失败: {e}"
            lprint(f"[l_notepad] ERROR: {msg}")
            # 注意：不在这里调用 print()，因为可能在后台线程
            return [], msg
    
    def _filter_and_sort_logs(self, logs: list, query: str = "") -> list:
        """按查询本地过滤并按 mtime 倒序（最新在上）排序。"""
        q = (query or "").strip().lower()
        if q:
            logs = [log for log in logs if q in log.path.lower()]

        def _key(log):
            try:
                return datetime.fromisoformat(log.mtime)
            except (ValueError, TypeError):
                return datetime.min

        return sorted(logs, key=_key, reverse=True)

    def _log_mtime_for(self, log_path: str) -> str | None:
        for log in getattr(self, "_server_logs_cache", None) or []:
            if log.path == log_path:
                return log.mtime
        return None

    def _fetch_server_logs_async(self, query: str = "") -> None:
        """后台获取服务器日志全量列表并缓存、刷新 UI。query 仅作占位，过滤在渲染时本地完成。"""
        import threading

        def _run_in_background():
            logs, error_msg = self._fetch_server_logs()
            QtCore.QMetaObject.invokeMethod(
                self,
                "_update_server_log_list",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(list, logs),
                QtCore.Q_ARG(str, error_msg or ""),
            )

        threading.Thread(target=_run_in_background, daemon=True).start()

    def _add_server_log_files_to_tree(
        self, tree: QtWidgets.QTreeWidget, query: str = ""
    ) -> None:
        """在文件树中添加服务器日志虚拟文件夹。"""
        cache = getattr(self, "_server_logs_cache", None)
        if cache is not None:
            logs, error_msg = cache, None
        else:
            logs, error_msg = self._fetch_server_logs()
            if not error_msg:
                self._server_logs_cache = list(logs)
        logs = self._filter_and_sort_logs(logs, query)

        # 始终显示文件夹，即使为空或失败
        log_folder = QtWidgets.QTreeWidgetItem(["\u25be \U0001f4cb \u670d\u52a1\u5668\u65e5\u5fd7"])
        log_folder.setData(0, QtCore.Qt.ItemDataRole.UserRole, SERVER_LOG_FOLDER_ID)
        log_folder.setData(0, QtCore.Qt.ItemDataRole.UserRole + 1, "\u670d\u52a1\u5668\u65e5\u5fd7")
        folder_font = log_folder.font(0)
        folder_font.setBold(True)
        log_folder.setFont(0, folder_font)
        log_folder.setForeground(0, QtGui.QBrush(QtGui.QColor("#F9A825")))
        log_folder.setBackground(
            0, QtGui.QBrush(QtGui.QColor("rgba(249, 168, 37, 0.06)"))
        )
        tree.addTopLevelItem(log_folder)

        # 失败时显示错误提示
        if error_msg:
            err_item = QtWidgets.QTreeWidgetItem([f"\u26a0 {error_msg}"])
            err_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, "__server_log_error__")
            err_item.setForeground(0, QtGui.QBrush(QtGui.QColor("#e57373")))
            log_folder.addChild(err_item)
            log_folder.setExpanded(True)
            return

        if not logs:
            return

        sub_folders: dict[str, QtWidgets.QTreeWidgetItem] = {}

        for log in logs:
            rel = PurePosixPath(log.path)
            parts = list(rel.parts)
            file_name = parts[-1]
            sub_parts = parts[:-1]

            parent_item = log_folder
            if sub_parts:
                cumulative: list[str] = []
                for sp in sub_parts:
                    cumulative.append(sp)
                    key = "/".join(cumulative)
                    if key not in sub_folders:
                        sub_item = QtWidgets.QTreeWidgetItem(
                            [f"\u25be \U0001f4c1 {sp}"]
                        )
                        sub_item.setData(
                            0,
                            QtCore.Qt.ItemDataRole.UserRole,
                            f"{SERVER_LOG_SUB_PREFIX}{key}",
                        )
                        sub_item.setData(
                            0, QtCore.Qt.ItemDataRole.UserRole + 1, sp
                        )
                        sf = sub_item.font(0)
                        sf.setBold(True)
                        sub_item.setFont(0, sf)
                        sub_item.setForeground(
                            0, QtGui.QBrush(QtGui.QColor("#F9A825"))
                        )
                        sub_item.setBackground(
                            0,
                            QtGui.QBrush(
                                QtGui.QColor("rgba(249, 168, 37, 0.04)")
                            ),
                        )
                        parent_item.addChild(sub_item)
                        sub_folders[key] = sub_item
                    parent_item = sub_folders[key]

            size_str = self._format_file_size(log.size)
            try:
                mtime_dt = datetime.fromisoformat(log.mtime)
                mtime = mtime_dt.strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                mtime = log.mtime

            item = QtWidgets.QTreeWidgetItem(
                [f"\U0001f4c4 {file_name}\n{mtime}  {size_str}"]
            )
            item.setData(
                0,
                QtCore.Qt.ItemDataRole.UserRole,
                f"{SERVER_LOG_PREFIX}{log.path}",
            )
            item.setToolTip(0, log.path)
            item.setForeground(
                0, QtGui.QBrush(QtGui.QColor("#d4d4d4"))
            )
            parent_item.addChild(item)

        log_folder.setExpanded(True)
        for sub_item in sub_folders.values():
            sub_item.setExpanded(True)

    def _add_server_log_files_to_list(self, query: str = "") -> None:
        """在文件列表中添加服务器日志虚拟文件夹（异步加载）。"""
        # 先显示占位符
        folder_header = QtWidgets.QListWidgetItem(
            "\U0001f4cb \u670d\u52a1\u5668\u65e5\u5fd7"
        )
        folder_header.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        folder_header.setData(
            QtCore.Qt.ItemDataRole.UserRole, SERVER_LOG_FOLDER_ID
        )
        folder_font = folder_header.font()
        folder_font.setBold(True)
        folder_header.setFont(folder_font)
        folder_header.setBackground(
            QtGui.QBrush(QtGui.QColor(255, 248, 225))
        )
        folder_header.setForeground(
            QtGui.QBrush(QtGui.QColor(200, 150, 0))
        )
        folder_header.setSizeHint(QtCore.QSize(0, 28))
        self.notes_list.addItem(folder_header)

        # 已有缓存则本地即时过滤渲染，避免每次输入都发起网络请求
        cache = getattr(self, "_server_logs_cache", None)
        if cache is not None:
            self._render_server_log_items(cache, query)
            return

        # 显示加载中的提示
        loading_item = QtWidgets.QListWidgetItem("  \U0001f504 \u6b63\u5728\u52a0\u8f7d\u670d\u52a1\u5668\u65e5\u5fd7\u5217\u8868...")
        loading_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        loading_item.setData(QtCore.Qt.ItemDataRole.UserRole, "__server_log_loading__")
        loading_item.setForeground(QtGui.QBrush(QtGui.QColor("#888888")))
        loading_item.setSizeHint(QtCore.QSize(0, 28))
        self._loading_item = loading_item  # 保存引用以便后续移除
        self.notes_list.addItem(loading_item)
        
        # 异步获取日志列表
        self._fetch_server_logs_async(query=query)
    
    @QtCore.Slot(list, str)
    def _update_server_log_list(self, logs: list, error_msg: str) -> None:
        """异步更新服务器日志列表 UI（在主线程中调用）。"""
        # 移除加载中的提示
        if hasattr(self, '_loading_item') and self._loading_item:
            idx = self.notes_list.row(self._loading_item)
            if idx >= 0:
                self.notes_list.takeItem(idx)
            self._loading_item = None
        
        if error_msg:
            err_item = QtWidgets.QListWidgetItem(f"  \u26a0 {error_msg}")
            err_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            err_item.setData(QtCore.Qt.ItemDataRole.UserRole, "__server_log_error__")
            err_item.setForeground(QtGui.QBrush(QtGui.QColor("#e57373")))
            err_item.setSizeHint(QtCore.QSize(0, 28))
            self.notes_list.addItem(err_item)
            return

        # 缓存全量列表，后续输入过滤纯本地完成
        self._server_logs_cache = list(logs)
        query = self.search_edit.text().strip() if self._qt_is_valid(getattr(self, "search_edit", None)) else ""
        self._render_server_log_items(logs, query)

    def _render_server_log_items(self, logs: list, query: str = "") -> None:
        """把日志条目渲染进 notes_list（本地过滤 + mtime 倒序）。"""
        logs = self._filter_and_sort_logs(logs, query)
        if not logs:
            # 显示空提示
            empty_item = QtWidgets.QListWidgetItem("  \U0001f4c2 \u672a\u627e\u5230\u670d\u52a1\u5668\u65e5\u5fd7")
            empty_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            empty_item.setForeground(QtGui.QBrush(QtGui.QColor("#888888")))
            empty_item.setSizeHint(QtCore.QSize(0, 28))
            self.notes_list.addItem(empty_item)
            return
        
        for log in logs:
            size_str = self._format_file_size(log.size)
            display_text = f"  \U0001f4c4 {log.path}\n  {size_str}"
            
            item = QtWidgets.QListWidgetItem(display_text)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setData(
                QtCore.Qt.ItemDataRole.UserRole,
                f"{SERVER_LOG_PREFIX}{log.path}",
            )
            item.setToolTip(log.path)
            item.setSizeHint(QtCore.QSize(0, 40))
            item.setTextAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            self.notes_list.addItem(item)

    def set_console_log_path(self, log_path: str) -> None:
        self._console_log_path = log_path
        self._log_file_path_label.setText(f"日志路径: {log_path}")
        self._log_file_path_label.setToolTip(log_path)
        self._start_console_log_tail()

    def _start_console_log_tail(self) -> None:
        if not self._console_log_path:
            return
        if getattr(self, "_console_log_tailer", None) is not None:
            return
        self._console_log_offset = 0
        self._console_log_buffer = ""
        self._console_log_tailer = QtCore.QTimer(self)
        self._console_log_tailer.setInterval(300)
        self._console_log_tailer.timeout.connect(self._poll_console_log)
        self._console_log_tailer.start()

    def _poll_console_log(self) -> None:
        if not self._console_log_path:
            return
        path = Path(self._console_log_path)
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(getattr(self, "_console_log_offset", 0))
                chunk = f.read()
                self._console_log_offset = f.tell()
        except Exception:
            return
        if not chunk:
            return
        self._console_log_buffer += chunk
        while "\n" in self._console_log_buffer:
            line, self._console_log_buffer = self._console_log_buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._append_console_line(line)

    def _append_console_line(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        if self._qt_is_valid(getattr(self, "log_view", None)):
            self.log_view.append_text_update_cache(
                f"[{ts}] {line}\n",
                LOG_VIEW_CONTENT_CACHE_KEY,
            )
            scrollbar = self.log_view.verticalScrollBar()
            if scrollbar:
                scrollbar.setValue(scrollbar.maximum())

    def _set_server_log_editor(self, log_path: str) -> None:
        """通过 API 打开服务器日志文件（可编辑）。"""
        self._ask_ai_mode = False
        self._current_ai_session_id = None
        self._current_external_file = None
        self._current_server_log_path = log_path
        self.state.current_note_id = None
        self._clear_loaded_local_file()
        content = self._get_log_content_cached(log_path)
        if content is None:
            return
        file_name = PurePosixPath(log_path).name
        mode = self._mode_from_filename(file_name)
        self.content_edit.setPlainText(content)
        self.content_edit.setReadOnly(False)
        self.title_edit.blockSignals(True)
        self.content_edit.blockSignals(True)
        self.title_edit.setText(f"\U0001f4cb {file_name}")
        self.title_edit.blockSignals(False)
        self.content_edit.blockSignals(False)
        self._set_right_panel_mode("external")
        self._set_code_editor_status_text(log_path, len(content.encode("utf-8")))
        self._set_ai_controls_visible(False)
        self.btn_ai_ask.setEnabled(False)
        self.btn_save.setEnabled(True)
        self.btn_delete.setEnabled(False)
        self.btn_favorite.setEnabled(False)
        self._auto_set_highlight_mode(file_name)
        self.state.dirty = False
        self._update_title()
        # 更新日志路径标签并启动服务器日志实时跟随
        self._log_file_path_label.setText(f"日志路径: {log_path}")
        self._log_file_path_label.setToolTip(log_path)
        self._start_server_log_tail(log_path)
        self._sync_version_combo_on_open()

    def _get_log_content_cached(self, log_path: str) -> str | None:
        """按 mtime 缓存日志内容；命中则免网络。失败返回 None（已弹错误）。"""
        mtime = self._log_mtime_for(log_path)
        cache = getattr(self, "_log_content_cache", None)
        if cache is None:
            cache = {}
            self._log_content_cache = cache
        hit = cache.get(log_path)
        if hit is not None and mtime is not None and hit[0] == mtime:
            return hit[1]
        try:
            data = self.api.get_log(log_path)
            content = data.get("content", "")
        except Exception as e:
            lprint(f"[l_notepad] ERROR: 获取日志文件失败: {e}")
            self._show_error(f"\u83b7\u53d6\u65e5\u5fd7\u6587\u4ef6\u5931\u8d25\uff1a{e}")
            return None
        cache[log_path] = (mtime, content)
        return content

    def _invalidate_log_content_cache(self, log_path: str) -> None:
        cache = getattr(self, "_log_content_cache", None)
        if cache:
            cache.pop(log_path, None)

    def _reload_server_logs(self) -> None:
        """强制清缓存并重新拉取服务器日志列表。"""
        self._server_logs_cache = None
        self._log_content_cache = {}
        self.refresh_notes()

    # ===== 服务器日志实时跟随（tail） =====

    def _start_server_log_tail(self, log_path: str) -> None:
        self._stop_server_log_tail()
        self._server_log_tail_path = log_path
        self._server_log_tail_busy = False
        timer = QtCore.QTimer(self)
        timer.setInterval(2000)
        timer.timeout.connect(self._poll_server_log_tail)
        timer.start()
        self._server_log_tailer = timer

    def _stop_server_log_tail(self) -> None:
        timer = getattr(self, "_server_log_tailer", None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._server_log_tailer = None
        self._server_log_tail_path = None

    def _poll_server_log_tail(self) -> None:
        import threading
        path = getattr(self, "_server_log_tail_path", None)
        if not path or self._current_server_log_path != path:
            self._stop_server_log_tail()
            return
        if getattr(self, "_server_log_tail_busy", False):
            return
        if self.state.dirty:  # 用户正在编辑，不覆盖
            return
        self._server_log_tail_busy = True

        def _work():
            try:
                data = self.api.get_log(path)
                content = data.get("content", "")
            except Exception:
                content = None
            QtCore.QMetaObject.invokeMethod(
                self,
                "_apply_server_log_tail",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, path),
                QtCore.Q_ARG(str, content if content is not None else "\x00"),
            )

        threading.Thread(target=_work, daemon=True).start()

    @QtCore.Slot(str, str)
    def _apply_server_log_tail(self, path: str, content: str) -> None:
        self._server_log_tail_busy = False
        if content == "\x00":  # 拉取失败
            return
        if self._current_server_log_path != path:
            return
        if self.state.dirty:
            return
        current = self._get_content_text()
        if content == current:
            return
        sb = self.content_edit.verticalScrollBar()
        at_bottom = sb is None or sb.value() >= sb.maximum() - 4
        self.content_edit.blockSignals(True)
        if content.startswith(current):
            cursor = self.content_edit.textCursor()
            self.content_edit.moveCursor(QtGui.QTextCursor.MoveOperation.End)
            self.content_edit.insertPlainText(content[len(current):])
            if not at_bottom:
                self.content_edit.setTextCursor(cursor)
        else:
            self.content_edit.setPlainText(content)
        self.content_edit.blockSignals(False)
        self.state.dirty = False
        self._invalidate_log_content_cache(path)
        if at_bottom and sb is not None:
            sb.setValue(sb.maximum())

    def _maybe_autosave_log_on_leave(self) -> None:
        """鼠标离开内容编辑区时，若正在编辑服务器日志且有改动则自动保存并生成版本。"""
        if not self._current_server_log_path:
            return
        if not self.state.dirty:
            return
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            return
        # 排除编辑区内部移动（如滑到滚动条）导致的误触发：仅当光标确实在编辑区外
        try:
            local = self.content_edit.mapFromGlobal(QtGui.QCursor.pos())
            if self.content_edit.rect().contains(local):
                return
        except Exception:
            pass
        self._save_server_log("离开编辑区")
        self._sync_version_combo_on_open()

    def _save_server_log(
        self,
        reason: str | None = None,
    ) -> None:
        """保存服务器日志文件到服务器。"""
        if not self._current_server_log_path:
            return
        log_path = self._current_server_log_path
        content = self._get_content_text()
        file_name = PurePosixPath(log_path).name
        try:
            self.api.update_log(log_path, content)
        except Exception as exc:
            lprint(f"[l_notepad] ERROR: 保存日志文件失败: {exc}")
            if reason:
                self.status.showMessage(f"保存日志失败：{exc}", 5000)
                lprint(f"保存日志失败：{exc}")
            else:
                self._show_error(f"保存日志文件失败：{exc}")
            return
        self.state.dirty = False
        self._update_title()
        self._invalidate_log_content_cache(log_path)
        self._record_version("log", log_path, file_name, content)
        if reason:
            self.status.showMessage(f"{reason} 已保存日志 {file_name}", 2500)
        else:
            self.status.showMessage(f"已保存日志 {file_name}", 2500)

    def _save_external_file(
        self,
        reason: str | None = None,
        *,
        notify_tray: bool = False,
        detail_ui: bool = False,
    ) -> None:
        if not self._current_external_file:
            return
        path = Path(self._current_external_file)
        filename = path.name
        # 外部文件已不存在（被删除/路径失效）：跳过保存，避免 [Errno 2]，温和提示不崩溃
        if not path.exists() or not path.is_file():
            lprint(f"外部文件不存在，跳过保存：{path}")
            self.status.showMessage(f"外部文件不存在，跳过保存：{filename}", 4000)
            self.state.dirty = False
            return
        content = self._get_content_text()
        # 安全防护：拒绝用空内容覆盖磁盘上非空的外部文件。
        # 重启/自动保存竞态下，外部文件可能尚未加载进编辑器（或预览源为空），
        # 此时写回会清空真实文件。仅当磁盘文件本就为空时才允许写空。
        if not content.strip():
            try:
                on_disk = path.read_text(encoding="utf-8") if path.exists() else ""
            except Exception:
                on_disk = ""
            if on_disk.strip():
                lprint(f"跳过外部文件保存：编辑器内容为空，避免清空非空文件 {path}")
                self.state.dirty = False
                return
        try:
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            if reason and (detail_ui or notify_tray):
                self._report_autosave(
                    reason,
                    ok=False,
                    filename=filename,
                    error=str(exc),
                    notify_tray=notify_tray,
                    detail_ui=detail_ui,
                )
            else:
                self.status.showMessage(f"保存外部文件失败：{exc}", 5000)
                lprint(f"保存外部文件失败：{exc}")
            return
        self.state.dirty = False
        self._save_external_files_state()
        self._record_loaded_local_file(path)
        self._record_version("external", self._current_external_file, filename, content)
        self._update_title()
        size_bytes, saved_at = self._stat_from_path_or_fallback(path)
        if reason and (detail_ui or notify_tray):
            self._report_autosave(
                reason,
                ok=True,
                filename=filename,
                size_bytes=size_bytes,
                saved_at=saved_at,
                notify_tray=notify_tray,
                detail_ui=detail_ui,
            )
        elif reason:
            self.status.showMessage(f"{reason} 已保存外部文件", 2500)
        else:
            self.status.showMessage("已保存外部文件", 2500)

    def _restart_app(self) -> None:
        try:
            self._auto_save_note("重启前")
            self._allow_close = True
            self._save_settings()
            self._save_top_window_state()
            if self._restart_callback is not None:
                self._restart_callback()
            else:
                subprocess.Popen([sys.executable, "-m", "l_notepad_client.local_main"])
            self.close()
            QtWidgets.QApplication.quit()
        except Exception as exc:
            self._allow_close = False
            self._show_error(f"重启失败: {exc}")

    def _save_top_window_state(self) -> None:
        """保存最顶层窗口（无边框外壳）的几何状态，供下次启动/重启恢复。

        无边框模式下本内容是嵌入到 L_FramelessMainWindow 外壳里的子部件，
        自身 saveGeometry() 只是子部件尺寸；只有外壳的 saveWindowState() 才会
        写入真正的窗口位置与大小。若直接 QApplication.quit()/close() 退出，
        外壳的 closeEvent 可能不会触发，需要在这里主动保存一次。
        """
        try:
            top = self.window()
        except Exception:
            return
        if top is None or top is self:
            return
        saver = getattr(top, "saveWindowState", None)
        if callable(saver):
            try:
                saver()
                # 立即落盘：紧接着就会 startDetached 启动新进程，新进程要靠它恢复几何
                top_settings = getattr(top, "_settings", None)
                if top_settings is not None:
                    top_settings.sync()
            except Exception as exc:
                lprint(f"保存顶层窗口状态失败: {exc}")

    def _toggle_favorite_current(self) -> None:
        if self._ask_ai_mode:
            lprint("当前处于 AI 模式，忽略置顶/收藏切换")
            return
        key = self._current_favorite_key()
        if key is None:
            lprint("当前没有可置顶的内容，忽略")
            return
        self._toggle_favorite_key(key)

    def _toggle_favorite_by_id(self, note_id: int) -> None:
        """切换指定笔记的收藏（收藏项全局置顶）。"""
        try:
            self._toggle_favorite_key(int(note_id))
        except (TypeError, ValueError):
            return

    def _toggle_favorite_key(self, key) -> None:
        """切换指定内容（笔记 int / 外部与 IPC 文件 str）的收藏并全局置顶。"""
        if key is None:
            return
        before = list(self._favorite_order)
        if key in self._favorite_order:
            self._favorite_order = [x for x in self._favorite_order if x != key]
            msg = "已取消收藏"
        else:
            self._favorite_order = [x for x in self._favorite_order if x != key]
            self._favorite_order.insert(0, key)
            msg = "已收藏并置顶"
        self._save_settings()
        self.refresh_notes()
        # 保持当前编辑内容被选中（笔记 / 外部 / IPC）
        if self._current_ipc_file:
            self._select_ipc_file(self._current_ipc_file)
        elif self._current_external_file:
            self._select_external_file(self._current_external_file)
        elif self.state.current_note_id is not None:
            self._select_note_id(int(self.state.current_note_id))
        self._update_favorite_button_label()
        self.status.showMessage(msg, 2000)
        lprint(f"收藏切换: key={key!r}, before={before}, after={self._favorite_order}")

    def _on_notes_rows_moved(self, *_args) -> None:
        if self._notes_tree_mode:
            return
        first = self.notes_list.item(0) if self.notes_list.count() else None
        if first is not None and first.data(QtCore.Qt.ItemDataRole.UserRole) != ASK_AI_ITEM_ID:
            self.refresh_notes()
            return
        # Drag-drop only updates ordering of already-favorited items.
        display_ids: list[int] = []
        for i in range(self.notes_list.count()):
            item = self.notes_list.item(i)
            note_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if note_id is None or note_id == ASK_AI_ITEM_ID:
                continue
            if not isinstance(note_id, int) or isinstance(note_id, bool):
                # 检索命中行等字符串角色不参与收藏排序
                continue
            display_ids.append(int(note_id))
        fav_set = set(self._favorite_order)
        if not fav_set:
            return
        reordered_favs = [x for x in display_ids if x in fav_set]
        if reordered_favs:
            self._favorite_order = reordered_favs
            self._save_settings()

    def _rename_note_from_item(self, item) -> None:
        item_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if isinstance(item, QtWidgets.QTreeWidgetItem) else item.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(item_id, str) and item_id.startswith(SEARCH_HIT_PREFIX):
            # 检索命中行（知识库/未映射笔记）：双击在默认浏览器打开对应网页
            self._open_search_hit(item_id)
            return
        if item_id == ASK_AI_ITEM_ID or (isinstance(item_id, str) and (
            item_id.startswith("__folder__:") or item_id.startswith("__empty__")
            or item_id.startswith(SERVER_LOG_PREFIX) or item_id == SERVER_LOG_FOLDER_ID
            or item_id.startswith(SERVER_LOG_SUB_PREFIX)
            or item_id == "__server_log_error__"
        )):
            return
        note_id = int(item_id)
        try:
            note = self.api.get_note(note_id)
        except ApiError as e:
            self._show_error(str(e))
            return

        new_title, ok = QtWidgets.QInputDialog.getText(
            self,
            "重命名日志文件",
            "新名称：",
            text=note.title,
        )
        if not ok:
            return
        new_title = (new_title or "").strip()
        if not new_title or new_title == note.title:
            return

        try:
            updated = self.api.update_note(note_id, title=new_title, content=note.content)
        except ApiError as e:
            self._show_error(str(e))
            return

        self.state.current_note_id = updated.id
        self.state.dirty = False
        self.refresh_notes()
        self.status.showMessage(f"已重命名：#{updated.id}", 2500)

    def _set_ai_editor(self, session_id: str | None = None) -> None:
        assert self.title_edit is not None
        assert self.btn_ai_ask is not None
        assert self.btn_save is not None
        assert self.btn_delete is not None
        assert self.btn_favorite is not None
        assert self.btn_new is not None
        assert self.ai_tabs is not None
        self._ask_ai_mode = True
        self._current_server_log_path = None
        self.state.current_note_id = None
        self.state.dirty = False

        # 切换到 AI 模式：显示 ai_tabs，隐藏普通编辑器
        lprint(f"进入 AI 模式，session_id={session_id!r}")
        self._set_right_panel_mode("ai")
        # 先确保至少有一个可见 AI 标签页，避免右侧布局没有任何内容变化
        if self.ai_tabs.count() == 0:
            lprint("AI 标签页当前为空，立即创建默认会话页")
            session = self._new_ai_session(select=True)
            self._current_ai_session_id = session.session_id
            lprint(f"默认会话页已创建: {session.session_id} / {session.title}")

        session = None
        if session_id and session_id in self._ai_sessions:
            session = self._ai_sessions[session_id]
            lprint(f"使用传入的 AI 会话: {session.session_id}")
        elif self._current_ai_session_id and self._current_ai_session_id in self._ai_sessions:
            session = self._ai_sessions[self._current_ai_session_id]
            lprint(f"使用当前 AI 会话: {session.session_id}")
        elif self.ai_tabs.count() > 0:
            tab_index = self.ai_tabs.currentIndex()
            if tab_index < 0:
                tab_index = 0
            widget = self.ai_tabs.widget(tab_index)
            tab_session_id = widget.property("session_id") if widget else None
            lprint(f"尝试从当前 AI 标签页获取会话: index={tab_index}, session_id={tab_session_id!r}")
            if tab_session_id and tab_session_id in self._ai_sessions:
                session = self._ai_sessions[tab_session_id]
        elif self._ai_sessions:
            session = self._sorted_ai_sessions()[0]
            lprint(f"从已有 AI 会话中选取: {session.session_id}")
        if session is None:
            lprint(f"未找到可复用 AI 会话，准备创建新会话；当前 sessions={len(self._ai_sessions)}, tabs={self.ai_tabs.count()}")
            session = self._new_ai_session(select=True)
            lprint(f"新建 AI 会话完成: {session.session_id} / {session.title}, tabs={self.ai_tabs.count()}")
        self._current_ai_session_id = session.session_id
        lprint(f"AI 当前会话: {session.session_id} / {session.title}")
        lprint(f"AI 标签页数量: {self.ai_tabs.count()}")
        lprint(f"AI tabs visible after select: {self.ai_tabs.isVisible()}")
        lprint(f"当前 AI 标签页索引: {self.ai_tabs.currentIndex()}")
        if self.ai_tabs.count() == 0:
            lprint("警告: AI 标签页数量为 0，尝试强制创建一个标签页")
            self._create_ai_tab(session)

        # 检查是否已有对应 session 的标签页
        tab_index = -1
        for i in range(self.ai_tabs.count()):
            widget = self.ai_tabs.widget(i)
            if widget and widget.property("session_id") == session.session_id:
                tab_index = i
                break

        if tab_index >= 0:
            # 切换到已有标签页
            lprint(f"定位到 AI 标签页索引: {tab_index}")
            self.ai_tabs.setCurrentIndex(tab_index)
            # 更新标签页内容
            self._update_ai_tab_content(session)
        else:
            lprint(f"未找到AI标签页，不自动创建: {session.title}")
            self._update_ai_tab_count()

        self.title_edit.blockSignals(True)
        self.title_edit.setText(session.title)
        self.title_edit.blockSignals(False)

        self._update_ai_ask_button_state()
        self.btn_save.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_favorite.setEnabled(False)
        self.btn_new.setEnabled(True)
        self._set_ai_controls_visible(True)
        self._update_title()
        self._update_realtime_token_stats()
        self._set_ai_status_bar(session)
        lprint("AI 模式切换完成")

    def _update_ai_tab_content(self, session: AiSession) -> None:
        """更新指定会话的标签页内容"""
        content_edit = self._get_ai_tab_content_edit(session.session_id)
        answer_edit = self._get_ai_tab_answer_edit(session.session_id)

        if content_edit:
            content_edit.blockSignals(True)
            draft = session.draft_prompt
            if _is_ai_prompt_placeholder_body(draft):
                draft = ""
                session.draft_prompt = ""
            _set_code_editor_document(content_edit, draft)
            content_edit.blockSignals(False)

        if answer_edit:
            answer_edit.blockSignals(True)
            _set_code_editor_document(answer_edit, self._render_ai_session_text(session))
            answer_edit.blockSignals(False)

    def _clear_ai_context(self) -> None:
        """清空当前问AI 会话的上下文（对话历史 + 回答区显示），保留输入框草稿。

        之后提问不再携带旧消息（`_ask_ai` 只把 session.messages 作为历史发出）。
        """
        if not self._ask_ai_mode or not self._current_ai_session_id:
            self.status.showMessage("当前不在问AI 会话中", 2500)
            return
        session = self._ai_sessions.get(self._current_ai_session_id)
        if session is None:
            self.status.showMessage("当前会话不存在", 2500)
            return
        if session.in_flight:
            self.status.showMessage("正在请求中，请等本轮结束后再清空上下文", 3000)
            return
        removed = len(session.messages)
        session.messages.clear()
        session.streaming_text = ""
        session.reasoning_text = ""
        self._update_ai_tab_content(session)
        self._update_token_labels()
        self._save_settings()
        self.status.showMessage(f"已清空对话上下文（{removed} 条消息）", 3000)
        lprint(f"清空问AI上下文：会话={session.title}，清除消息 {removed} 条")

    def _ask_ai(self) -> None:
        if not self._ask_ai_mode or not self._current_ai_session_id:
            return
        session = self._ai_sessions.get(self._current_ai_session_id)
        if session is None:
            return
        if session.in_flight:
            # 回车连按 / 重复点击：本轮未结束就不再发
            self.status.showMessage("正在请求中，请稍候", 2000)
            return
        # 使用当前标签页的内容编辑器
        content_edit = self._get_ai_tab_content_edit(self._current_ai_session_id)
        if content_edit is None:
            return
        prompt = content_edit.toPlainText().strip()
        provider_name = self.provider_combo.currentText()
        if provider_name not in AI_PROVIDERS:
            provider_name = DEFAULT_PROVIDER
        api_url, api_key, _, default_model = AI_PROVIDERS[provider_name]
        model = self.model_combo.currentText().strip() or default_model
        if not prompt:
            self.status.showMessage("请输入问题", 2500)
            return
        if not api_key:
            self.status.showMessage(f"未配置 {provider_name} API Key", 5000)
            return

        self._ai_request_seq += 1
        request_id = self._ai_request_seq
        self._active_ai_request_id = request_id
        session.in_flight = True
        session.streaming_text = ""
        session.reasoning_text = ""
        session.draft_prompt = prompt
        self.btn_ai_ask.setEnabled(False)
        self.btn_ai_ask.setText("请求中...")
        self._ai_input_tokens = self._estimate_tokens(prompt)
        self._ai_output_tokens = 0
        self._ai_stream_text = ""
        # 更新标签页的回答显示
        answer_edit = self._get_ai_tab_answer_edit(self._current_ai_session_id)
        if answer_edit:
            _set_code_editor_document(answer_edit, self._render_ai_session_text(session))
        self._update_token_labels()
        self.status.showMessage(f"正在请求模型：{model}", 2500)
        lprint(f"问AI请求已发送，模型：{model}，会话：{session.title}")

        def _worker() -> None:
            history = session.messages[-20:] if session.messages else []
            payload = {
                "model": model,
                "stream": True,
                "messages": [
                    {"role": "system", "content": "你是一个简洁、可靠的中文助手。"},
                ] + history + [{"role": "user", "content": prompt}],
            }
            req = urllib.request.Request(
                api_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            try:
                with _SILICONFLOW_OPENER.open(req, timeout=60) as resp:
                    chunks: list[str] = []
                    for raw in resp:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_text = line[5:].strip()
                        if data_text == "[DONE]":
                            break
                        try:
                            data = json.loads(data_text)
                        except Exception:
                            continue
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        reasoning = delta.get("reasoning_content") or ""
                        if reasoning:
                            self._ai_bridge.reasoning_chunk.emit(request_id, session.session_id, reasoning)
                        chunk = delta.get("content") or ""
                        if chunk:
                            chunks.append(chunk)
                            self._ai_bridge.chunk.emit(request_id, session.session_id, chunk)
                    content = "".join(chunks).strip()
                if not content:
                    content = "[接口返回为空]"
                self._ai_bridge.finished.emit(request_id, session.session_id, True, content, prompt)
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
                self._ai_bridge.finished.emit(
                    request_id,
                    session.session_id,
                    False,
                    f"HTTPError {exc.code}: {body}",
                    prompt,
                )
            except Exception as exc:
                self._ai_bridge.finished.emit(request_id, session.session_id, False, repr(exc), prompt)

        threading.Thread(target=_worker, daemon=True).start()

    def _estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        ascii_words = len(re.findall(r"[A-Za-z0-9_]+", text))
        punct = len(re.findall(r"[^\sA-Za-z0-9_\u4e00-\u9fff]", text))
        return max(1, chinese_chars + ascii_words + max(0, punct // 2))

    def _update_realtime_token_stats(self) -> None:
        if not self._ask_ai_mode:
            return
        # 使用当前标签页的内容编辑器
        content_edit = self._get_ai_tab_content_edit(self._current_ai_session_id)
        if content_edit is None:
            return
        if self._current_ai_session_id and self._current_ai_session_id in self._ai_sessions:
            self._ai_sessions[self._current_ai_session_id].draft_prompt = content_edit.toPlainText()
        self._ai_input_tokens = self._estimate_tokens(content_edit.toPlainText())
        self._update_token_labels()
        self._update_ai_ask_button_state()

    def _update_ai_ask_button_state(self) -> None:
        if self.btn_ai_ask is None:
            return
        self.btn_ai_ask.setEnabled(True)
        self.btn_ai_ask.setText("问AI")

    def _current_model_price(self) -> ModelPrice | None:
        model = self.model_combo.currentText().strip()
        candidates = [model, model.split("/")[-1]]
        normalized = {self._normalize_model_name(x): x for x in self._model_prices}
        for candidate in candidates:
            if candidate in self._model_prices:
                return self._model_prices[candidate]
            key = self._normalize_model_name(candidate)
            if key in normalized:
                return self._model_prices[normalized[key]]
        return None

    @staticmethod
    def _normalize_model_name(model: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", model.lower())

    def _update_token_labels(self) -> None:
        label_input = self._get_right_widget("label_input_tokens", expected_type=QtWidgets.QLabel)
        label_output = self._get_right_widget("label_output_tokens", expected_type=QtWidgets.QLabel)
        label_cost = self._get_right_widget("label_cost", expected_type=QtWidgets.QLabel)
        if not (self._qt_is_valid(label_input) and self._qt_is_valid(label_output) and self._qt_is_valid(label_cost)):
            lprint("token label 已失效，跳过更新")
            return
        label_input.setText(f"输入: {self._ai_input_tokens} tokens")
        label_output.setText(f"输出: {self._ai_output_tokens} tokens")
        price = self._current_model_price()
        if price is None:
            label_cost.setText("费用: 价格未知")
            return
        input_cost = self._ai_input_tokens / 1_000_000 * price.input_per_m
        output_cost = self._ai_output_tokens / 1_000_000 * price.output_per_m
        total = input_cost + output_cost
        label_cost.setText(
            f"费用: {price.currency}{total:.6f} "
            f"(入 {price.currency}{input_cost:.6f} / 出 {price.currency}{output_cost:.6f})"
        )

    def _update_model_list_for_provider(self, provider_name: str) -> None:
        """根据提供商更新模型列表。"""
        if provider_name not in AI_PROVIDERS:
            provider_name = DEFAULT_PROVIDER
        _, _, models, default_model = AI_PROVIDERS[provider_name]
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(default_model)

    def _on_provider_changed(self, provider_name: str) -> None:
        """提供商切换时更新模型列表并保存设置。"""
        if provider_name not in AI_PROVIDERS:
            return
        self._settings.setValue("ai/provider", provider_name)
        self._settings.sync()
        self._update_model_list_for_provider(provider_name)
        lprint(f"AI提供商已切换：{provider_name}")

    def _on_ai_model_changed(self, model: str) -> None:
        model = model.strip()
        if not model:
            return
        self._selected_ai_model = model
        self._settings.setValue("ai/model", model)
        self._settings.sync()
        lprint(f"AI模型已选择：{model}")

    def _refresh_ai_models(self) -> None:
        provider_name = self.provider_combo.currentText()
        if provider_name != "SiliconFlow":
            self.status.showMessage(f"刷新模型列表仅支持 SiliconFlow，{provider_name} 请使用预设列表", 4000)
            return
        if not SILICONFLOW_API_KEY:
            self.status.showMessage("未配置 SiliconFlow API Key", 5000)
            return
        self.btn_refresh_models.setEnabled(False)
        self.btn_refresh_models.setText("刷新中...")
        self.status.showMessage("正在读取硅基模型列表...", 2500)

        def _worker() -> None:
            url = f"{SILICONFLOW_MODELS_URL}?type=text&sub_type=chat"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {SILICONFLOW_API_KEY}"},
                method="GET",
            )
            try:
                with _SILICONFLOW_OPENER.open(req, timeout=30) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                data = json.loads(text)
                models = []
                prices: dict[str, ModelPrice] = {}
                for item in data.get("data", []):
                    model_id = str(item.get("id", "")).strip()
                    if model_id:
                        models.append(model_id)
                        price = self._extract_price_from_model_item(item)
                        if price is not None:
                            prices[model_id] = price
                if not models:
                    raise RuntimeError(f"模型列表为空: {text[:500]}")
                self._model_bridge.finished.emit(True, {"models": sorted(set(models)), "prices": prices})
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
                self._model_bridge.finished.emit(False, f"HTTPError {exc.code}: {body}")
            except Exception as exc:
                self._model_bridge.finished.emit(False, repr(exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_models_loaded(self, ok: bool, payload: object) -> None:
        self.btn_refresh_models.setEnabled(True)
        self.btn_refresh_models.setText("刷新模型")
        if not ok:
            self.status.showMessage("读取模型列表失败", 5000)
            lprint(f"读取模型列表失败：{payload}")
            return
        if isinstance(payload, dict):
            raw_models = payload.get("models", [])
            raw_prices = payload.get("prices", {})
            if isinstance(raw_prices, dict):
                self._model_prices.update(raw_prices)
        else:
            raw_models = payload
        models = [str(x) for x in raw_models if str(x).strip()]
        current = self.model_combo.currentText().strip() or self._selected_ai_model
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(models)
        if current in models:
            self.model_combo.setCurrentText(current)
        else:
            self.model_combo.setCurrentText(models[0])
        self.model_combo.blockSignals(False)
        self._on_ai_model_changed(self.model_combo.currentText())
        self.status.showMessage(f"已读取 {len(models)} 个模型", 3000)
        lprint(f"硅基模型列表已刷新：{len(models)} 个")
        self._update_token_labels()

    def _extract_price_from_model_item(self, item: dict) -> ModelPrice | None:
        def _number(value) -> float | None:
            if value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            match = re.search(r"\d+(?:\.\d+)?", str(value))
            return float(match.group(0)) if match else None

        containers = [item]
        for key in ("pricing", "price", "billing"):
            value = item.get(key)
            if isinstance(value, dict):
                containers.append(value)
        input_value = output_value = None
        for data in containers:
            input_value = input_value or _number(
                data.get("input")
                or data.get("input_price")
                or data.get("prompt")
                or data.get("prompt_price")
            )
            output_value = output_value or _number(
                data.get("output")
                or data.get("output_price")
                or data.get("completion")
                or data.get("completion_price")
            )
        if input_value is None or output_value is None:
            return None
        return ModelPrice(input_per_m=input_value, output_per_m=output_value)

    def _init_account_favorites(self) -> None:
        """初始化账号收藏标签页"""
        self._account_favorites_panel = self.findChild(AccountFavoritesPanel, "tab_account_favorites")
        if self._account_favorites_panel is None:
            self._account_favorites_panel = AccountFavoritesPanel(self)
        else:
            # .ui 已加载，确保延迟初始化已触发
            if hasattr(self._account_favorites_panel, "finalize_ui"):
                self._account_favorites_panel.finalize_ui()
        
        # 注入 API 并从 PostgreSQL 重新加载账号数据（失败自动回退本地 JSON）
        if hasattr(self._account_favorites_panel, "api"):
            self._account_favorites_panel.api = self.api
            if hasattr(self._account_favorites_panel, "reload_data"):
                self._account_favorites_panel.reload_data()
        
        # 将组件添加到标签页（如果还没有添加）
        if hasattr(self, 'tabs') and self.tabs:
            # 查找账号收藏标签页的索引
            account_tab_index = -1
            for i in range(self.tabs.count()):
                widget = self.tabs.widget(i)
                if widget is self._account_favorites_panel:
                    account_tab_index = i
                    break
            
            # 如果还没有添加，插入到合适的位置（例如在文件夹收藏之后）
            if account_tab_index < 0:
                # 找到网址收藏标签页之后的位置
                insert_index = 4  # 默认插入到第5个位置
                if hasattr(self, '_url_favorites_tab_index') and self._url_favorites_tab_index >= 0:
                    insert_index = self._url_favorites_tab_index + 1
                
                self.tabs.insertTab(insert_index, self._account_favorites_panel, "👤 账号")
    
    def _refresh_official_prices(self) -> None:
        """刷新硅基流动官方模型价格

        优先国内站（.cn，下载快且稳定），解析嵌入的转义 JSON 价格记录；
        国内站失败再尝试国际站（.com，原正则格式）。
        注：.com 站在慢网络下会被服务端约 19s 断连，1.3MB 页面无法下载完整。
        """
        def _try_fetch(url: str, parse, max_attempts: int) -> tuple[bool, object]:
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        method="GET",
                    )
                    with _SILICONFLOW_OPENER.open(req, timeout=20) as resp:
                        html = resp.read().decode("utf-8", errors="replace")
                    prices = parse(html)
                    if prices:
                        return True, prices
                    last_exc = RuntimeError("价格页未解析到模型价格")
                except Exception as exc:
                    last_exc = exc
                    # IncompleteRead 时已下载部分可能已含价格数据，尝试降级解析
                    partial = getattr(exc, "partial", None)
                    if partial:
                        try:
                            prices = parse(partial.decode("utf-8", errors="replace"))
                        except Exception:
                            prices = {}
                        if prices:
                            return True, prices
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
            return False, repr(last_exc)

        def _worker() -> None:
            ok, payload = _try_fetch(SILICONFLOW_PRICING_CN_URL, self._parse_cn_pricing_page, 2)
            if not ok:
                ok, payload = _try_fetch(SILICONFLOW_PRICING_URL, self._parse_official_pricing_page, 1)
            self._price_bridge.finished.emit(ok, payload)

        threading.Thread(target=_worker, daemon=True).start()

    def _parse_cn_pricing_page(self, text: str) -> dict[str, ModelPrice]:
        """解析硅基国内站（.cn）价格页：价格数据嵌在 Next.js RSC 的转义 JSON 中。

        记录字段形如：
        \\"playgroundName\\":\\"zai-org/GLM-5.2\\" ... \\"componentCode\\":\\"input-tokens\\"
        ... \\"realTimePriceCnyUnit\\":\\"0.008000\\" \\"unitZhCnName\\":\\"K tokens\\"
        单位为 CNY/K tokens，换算成 CNY/M tokens（×1000）后与 ModelPrice 语义对齐。
        """
        prices: dict[str, ModelPrice] = {}
        pattern = re.compile(
            r'\\"playgroundName\\":\\"(?P<name>[^"\\]{3,120})\\"'
            r'(?P<seg>(?:(?!\\"playgroundName\\").)*?)'
            r'\\"componentCode\\":\\"(?P<code>input-tokens|output-tokens)\\"'
            r'(?P<seg2>(?:(?!\\"playgroundName\\").)*?)'
            r'\\"realTimePriceCnyUnit\\":\\"(?P<price>\d+(?:\.\d+)?)\\"'
            r'(?P<seg3>(?:(?!\\"playgroundName\\").)*?)'
            r'\\"unitZhCnName\\":\\"(?P<unit>[^"\\]*)\\"',
            re.DOTALL,
        )
        pending: dict[str, dict[str, float]] = {}
        for match in pattern.finditer(text):
            name = match.group("name").strip()
            unit = match.group("unit")
            # 只统计文本模型的 token 计费，跳过图片/视频/字符单位；
            # K tokens → ×1000 换算成 M tokens，M tokens 直接使用
            if unit == "K tokens":
                factor = 1000.0
            elif unit == "M tokens":
                factor = 1.0
            else:
                continue
            try:
                price_per_unit = float(match.group("price"))
            except ValueError:
                continue
            entry = pending.setdefault(name, {"in": 0.0, "out": 0.0})
            key = "in" if match.group("code") == "input-tokens" else "out"
            entry[key] = price_per_unit * factor
        for name, entry in pending.items():
            if entry["in"] or entry["out"]:
                prices[name] = ModelPrice(
                    input_per_m=entry["in"],
                    output_per_m=entry["out"],
                    currency="¥",
                )
        return prices

    def _parse_official_pricing_page(self, text: str) -> dict[str, ModelPrice]:
        prices: dict[str, ModelPrice] = {}
        compact = re.sub(r"\s+", " ", text)
        pattern = re.compile(
            r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.\-\[\] ]{2,80}),\s*"
            r"(?P<context>\d+(?:\.\d+)?K),\s*"
            r"(?P<input>\d+(?:\.\d+)?)(?:,\s*(?P<cached>\d+(?:\.\d+)?))?,\s*"
            r"(?P<output>\d+(?:\.\d+)?)\s+\[Details\]",
            re.IGNORECASE,
        )
        for match in pattern.finditer(compact):
            name = match.group("name").strip()
            try:
                prices[name] = ModelPrice(
                    input_per_m=float(match.group("input")),
                    output_per_m=float(match.group("output")),
                    currency="$",
                )
            except Exception:
                continue
        return prices

    def _on_prices_loaded(self, ok: bool, payload: object) -> None:
        if not self._qt_is_valid(getattr(self, "label_price_source", None)):
            lprint(f"价格控件已失效，跳过更新：ok={ok}, payload={payload!r}")
            return
        if not ok:
            self.label_price_source.setText("价格: 官网读取失败")
            lprint(f"官网价格读取失败：{payload}")
            return
        if isinstance(payload, dict):
            self._model_prices.update(payload)
        self.label_price_source.setText(f"价格: 硅基官网 {len(self._model_prices)} 个")
        lprint(f"已从硅基官网读取价格：{len(self._model_prices)} 个")
        self._update_token_labels()

    def _hide_thinking_block(self, answer_edit) -> None:
        """回答结束后自动折叠 <思考过程>...</思考过程> 行块（含标记行）。
        折叠后行号区显示 ▶ N行 指示器，单击可展开。"""
        try:
            inner = answer_edit.editor()
        except AttributeError:
            return
        lines = inner.toPlainText().split("\n")
        start_line: int | None = None
        end_line: int | None = None
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "<思考过程>" and start_line is None:
                start_line = i
            elif stripped == "</思考过程>" and start_line is not None:
                end_line = i
                break
        if start_line and end_line:
            inner.hide_lines(start_line, end_line)

    def _on_ai_finished(self, request_id: int, session_id: str, ok: bool, message: str, prompt: str) -> None:
        session = self._ai_sessions.get(session_id)
        if session is None:
            return
        session.in_flight = False
        if request_id == self._active_ai_request_id:
            self._active_ai_request_id = None
        if self._current_ai_session_id == session_id:
            self.btn_ai_ask.setEnabled(True)
            self.btn_ai_ask.setText("问AI")
        prefix = "AI回答" if ok else "问AI失败"
        if ok:
            session.messages.append({"role": "user", "content": prompt})
            session.messages.append({"role": "assistant", "content": message})
            session.draft_prompt = ""
            session.streaming_text = ""
            self._ai_output_tokens = self._estimate_tokens(message)
            if self._current_ai_session_id == session_id:
                # 清空当前标签页的内容编辑器
                content_edit = self._get_ai_tab_content_edit(session_id)
                if content_edit:
                    content_edit.blockSignals(True)
                    content_edit.clear()
                    content_edit.blockSignals(False)
                # 更新回答显示
                answer_edit = self._get_ai_tab_answer_edit(session_id)
                if answer_edit:
                    _set_code_editor_document(
                        answer_edit, self._render_ai_session_text(session)
                    )
                    # 回答完毕后折叠思考过程
                    self._hide_thinking_block(answer_edit)
        else:
            session.streaming_text = f"{prefix}:\n{message}"
            if self._current_ai_session_id == session_id:
                answer_edit = self._get_ai_tab_answer_edit(session_id)
                if answer_edit:
                    _set_code_editor_document(
                        answer_edit, self._render_ai_session_text(session)
                    )
        self._update_token_labels()
        self.status.showMessage(prefix, 3500)
        lprint(f"{prefix}（{session.title}）")
        self._save_settings()

    def _on_ai_chunk(self, request_id: int, session_id: str, chunk: str) -> None:
        if request_id != self._active_ai_request_id:
            return
        session = self._ai_sessions.get(session_id)
        if session is None:
            return
        session.streaming_text += chunk
        if self._current_ai_session_id == session_id:
            self._ai_stream_text = session.streaming_text
            self._ai_output_tokens = self._estimate_tokens(session.streaming_text)
            # 使用标签页的回答编辑器
            answer_edit = self._get_ai_tab_answer_edit(session_id)
            if answer_edit:
                _set_code_editor_document(
                    answer_edit, self._render_ai_session_text(session)
                )
                cursor = answer_edit.textCursor()
                cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                answer_edit.setTextCursor(cursor)
            self._update_token_labels()

    def _on_ai_reasoning_chunk(self, request_id: int, session_id: str, chunk: str) -> None:
        """流式推理内容（reasoning_content）累积并刷新显示。"""
        if request_id != self._active_ai_request_id:
            return
        session = self._ai_sessions.get(session_id)
        if session is None:
            return
        session.reasoning_text += chunk
        if self._current_ai_session_id == session_id:
            answer_edit = self._get_ai_tab_answer_edit(session_id)
            if answer_edit:
                _set_code_editor_document(
                    answer_edit, self._render_ai_session_text(session)
                )
                cursor = answer_edit.textCursor()
                cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                answer_edit.setTextCursor(cursor)

    def _set_editor(self, note: NoteDto | None) -> None:
        self._ask_ai_mode = False
        self._current_ai_session_id = None
        self._current_external_file = None
        self._current_ipc_file = None
        self._current_server_log_path = None
        self._set_right_panel_mode("empty" if note is None else "note")
        if self._qt_is_valid(getattr(self, "ai_answer_edit", None)):
            self.ai_answer_edit.clear()
        if self._qt_is_valid(getattr(self, "btn_ai_ask", None)):
            self.btn_ai_ask.setEnabled(False)
        if self._qt_is_valid(getattr(self, "btn_save", None)):
            self.btn_save.setEnabled(True)
        if self._qt_is_valid(getattr(self, "btn_delete", None)):
            self.btn_delete.setEnabled(True)
        if self._qt_is_valid(getattr(self, "btn_favorite", None)):
            self.btn_favorite.setEnabled(True)
        self._refresh_core_widget_refs()
        self._set_ai_controls_visible(False)
        title_edit_widget = self._get_right_widget("title_edit", "title_edit")
        content_edit_widget = self._get_right_widget("content_edit", "content_edit")
        title_edit_valid = self._qt_is_valid(title_edit_widget)
        content_edit_valid = self._qt_is_valid(content_edit_widget)
        if title_edit_valid:
            title_edit_widget.blockSignals(True)
        if content_edit_valid:
            content_edit_widget.blockSignals(True)
        expected_content = ""
        try:
            if note is None:
                lprint("准备刷新右侧内容: 空编辑器")
                if title_edit_valid:
                    title_edit_widget.setText("")
                if content_edit_valid:
                    content_edit_widget.clear()
                self._set_code_editor_status_text("未加载文件")
                self.state.current_note_id = None
            else:
                lprint(
                    "准备刷新右侧内容: "
                    f"note_id={note.id}, title={note.title!r}, api_chars={len(note.content or '')}"
                )
                if title_edit_valid:
                    title_edit_widget.setText(note.title)
                note_path = self._note_file_path(note.title)
                if content_edit_valid and note_path.is_file() and note_path.suffix.lower() == ".log":
                    mode = self._mode_from_filename(note.title)
                    loaded = content_edit_widget.load_text_file_cached(note_path, mode=mode)
                    self._set_code_editor_status_file(note_path)
                    expected_content = content_edit_widget.toPlainText()
                    self._set_code_editor_status_text(str(note_path), len(expected_content.encode("utf-8")))
                    lprint(
                        "日志文件缓存加载: "
                        f"path={str(note_path)!r}, loaded={loaded}, chars={len(expected_content)}"
                    )
                else:
                    expected_content = note.content or ""
                    if content_edit_valid:
                        content_edit_widget.setPlainText(expected_content)
                    self._set_code_editor_status_text(str(note_path), len(expected_content.encode("utf-8")))
                self.state.current_note_id = note.id
                # 根据文件扩展名自动切换高亮模式
                self._auto_set_highlight_mode(note.title)
        finally:
            if title_edit_valid:
                title_edit_widget.blockSignals(False)
            if content_edit_valid:
                content_edit_widget.blockSignals(False)
        self._set_right_panel_mode("empty" if note is None else "note")
        self._remember_right_widget("title_edit", title_edit_widget)
        self._remember_right_widget("content_edit", content_edit_widget)
        if note is not None:
            self._record_loaded_local_file(self._note_file_path(note.title))
        else:
            self._clear_loaded_local_file()
        self._verify_right_note_content(note, expected_content)
        self.state.dirty = False
        self._update_title()
        self._update_token_labels()
        self._sync_version_combo_on_open()

    def _verify_right_note_content(self, note: NoteDto | None, expected_content: str) -> None:
        """验证右侧编辑器是否已经显示目标笔记内容，并输出诊断日志。"""
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            lprint("右侧内容验证跳过：content_edit 已失效")
            return
        actual_content = self.content_edit.toPlainText()
        previous_sig = getattr(self, "_last_right_content_signature", None)
        title_text = self.title_edit.text() if self._qt_is_valid(getattr(self, "title_edit", None)) else ""
        current_sig = (self.state.current_note_id, title_text, len(actual_content), actual_content[:80])
        changed = previous_sig != current_sig
        matches_expected = actual_content == (expected_content or "")
        note_id = None if note is None else note.id
        note_title = "" if note is None else note.title
        self._last_right_content_signature = current_sig
        parent_chain: list[str] = []
        parent = self.content_edit.parentWidget()
        while parent is not None and len(parent_chain) < 6:
            name = parent.objectName() or parent.__class__.__name__
            parent_chain.append(f"{name}:visible={parent.isVisible()},hidden={parent.isHidden()}")
            parent = parent.parentWidget()
        lprint(
            "右侧内容验证: "
            f"note_id={note_id}, title={note_title!r}, "
            f"expected_chars={len(expected_content or '')}, actual_chars={len(actual_content)}, "
            f"matches_expected={matches_expected}, changed={changed}, "
            f"visible={self.content_edit.isVisible()}, hidden={self.content_edit.isHidden()}, "
            f"parents={' > '.join(parent_chain)}, "
            f"preview={actual_content[:60]!r}"
        )

    def _mark_dirty(self) -> None:
        if self._ask_ai_mode:
            self._update_realtime_token_stats()
            return
        if not self.state.dirty:
            self.state.dirty = True
            self._update_title()

    def _confirm_discard(self) -> bool:
        ret = QtWidgets.QMessageBox.question(self, "未保存更改", "当前笔记有未保存更改，是否丢弃？")
        return ret == QtWidgets.QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._auto_save_note("窗口关闭")
        except Exception as e:
            lprint(f"窗口关闭时自动保存失败: {e}")
        
        if self._allow_close:
            self._save_settings()
            event.accept()
            return
        
        self._save_settings()
        self.save_window_position()
        self.hide()
        event.ignore()

    def on_tray_message_log(self, message: str) -> None:
        lprint(message)

    # ===== 本地文件（笔记 / 外部文件）外部修改检测 =====

    def _current_local_file_path(self) -> Path | None:
        """返回当前编辑区对应的磁盘文件路径（笔记 / 外部 / IPC 文件）。

        服务器日志、问AI 会话等非本地文件返回 None。
        """
        if self._ask_ai_mode or self._current_server_log_path:
            return None
        for raw in (self._current_external_file, self._current_ipc_file):
            if raw:
                return Path(raw)
        if self.state.current_note_id is not None:
            title = (
                self.title_edit.text().strip()
                if self._qt_is_valid(getattr(self, "title_edit", None))
                else ""
            )
            if title:
                return self._note_file_path(title)
        return None

    def _clear_loaded_local_file(self) -> None:
        self._loaded_local_file_path = None
        self._loaded_local_file_mtime = None
        self._loaded_local_file_size = None
        self._refresh_local_file_watch()

    @staticmethod
    def _local_file_state(path: Path) -> tuple[float, int] | None:
        """返回 (mtime, size)；文件不存在/不可访问返回 None。"""
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def _record_loaded_local_file(self, path: Path | None = None) -> None:
        """记录当前本地文件的加载/保存快照（路径 + mtime + 大小），并重挂监视。"""
        target = path if path is not None else self._current_local_file_path()
        if target is None:
            self._clear_loaded_local_file()
            return
        state = self._local_file_state(target)
        self._loaded_local_file_path = str(target)
        self._loaded_local_file_mtime = state[0] if state else None
        self._loaded_local_file_size = state[1] if state else None
        self._refresh_local_file_watch()
        lprint(
            f"[本地文件监控] 记录快照 path={target} "
            f"mtime={self._loaded_local_file_mtime} size={self._loaded_local_file_size}"
        )

    def _refresh_local_file_watch(self) -> None:
        """让文件监视/轮询对齐当前本地文件（含父目录，兼容原子替换写盘）。"""
        watcher = getattr(self, "_local_file_watcher", None)
        timer = getattr(self, "_local_file_poll_timer", None)
        if watcher is None or timer is None:
            return
        existing = list(watcher.files()) + list(watcher.directories())
        if existing:
            watcher.removePaths(existing)
        target = self._current_local_file_path()
        if target is None:
            timer.stop()
            return
        watcher.addPath(str(target))
        if target.parent.exists():
            watcher.addPath(str(target.parent))
        timer.start()
        self._ensure_top_window_activation_filter()

    def _ensure_top_window_activation_filter(self) -> None:
        """在顶层窗口上挂钩激活事件。

        本窗口是无边框外壳（L_FramelessMainWindow）里的内容子控件，
        Qt 的 WindowActivate/ActivationChange 只发给顶层窗口，子控件收不到，
        因此必须在 ``self.window()`` 上过滤，否则「切回程序即检查」永不触发。
        """
        if getattr(self, "_top_window_activation_filter", False):
            return
        top = self.window()
        if top is None or top is self:
            return
        top.installEventFilter(self)
        self._top_window_activation_filter = True

    def _on_local_file_watch_event(self, _path: str) -> None:
        """QFileSystemWatcher 回调：文件或其目录变化 → 复核快照。"""
        self._check_local_file_changed(source="文件监视")

    def _poll_local_file_changed(self) -> None:
        """轮询兜底：监视失效（原子替换写盘等）时仍能发现外部修改。"""
        self._check_local_file_changed(source="轮询")

    def _check_local_file_updated_on_foreground(self) -> None:
        """回到前台时，若当前本地文件被外部修改则提示是否重载。"""
        self._check_local_file_changed(source="窗口激活", force=True)

    def _check_local_file_changed(self, *, source: str = "", force: bool = False) -> None:
        """比对当前本地文件的快照与磁盘状态，发现外部修改则提示重载。

        非强制入口（文件监视/轮询）只在窗口可见且处于活动状态时提示，
        避免在后台弹出模态框打断其它程序；窗口激活时强制检查一次。
        """
        if getattr(self, "_local_file_reload_prompting", False):
            return
        if not force and not (self.isVisible() and self.isActiveWindow()):
            return
        target = self._current_local_file_path()
        if target is None:
            return
        if str(target) != self._loaded_local_file_path:
            # 切换了文件：重新记录快照，不提示
            self._record_loaded_local_file(target)
            return
        state = self._local_file_state(target)
        if state is None:
            if self._loaded_local_file_mtime is not None:
                self._loaded_local_file_mtime = None
                self._loaded_local_file_size = None
                self.status.showMessage(f"文件已不存在：{target.name}", 4000)
                lprint(f"[本地文件监控] 文件已不存在 path={target}")
                # 列表条目立刻标 ✕（点底部「刷新」可清理该条目）
                self.refresh_notes()
            return
        mtime, size = state
        last_mtime = self._loaded_local_file_mtime
        last_size = self._loaded_local_file_size
        if last_mtime is None:
            self._record_loaded_local_file(target)
            return
        if mtime <= last_mtime and (last_size is None or size == last_size):
            return
        name = target.name
        dirty = bool(getattr(self.state, "dirty", False))
        lprint(
            f"[本地文件监控] 检测到外部修改 source={source} path={target} "
            f"mtime={last_mtime}->{mtime} size={last_size}->{size} dirty={dirty}"
        )
        if dirty:
            text = (
                f"文件『{name}』已在磁盘上被外部修改。\n"
                f"重载会丢弃你当前未保存的修改，是否重载？"
            )
        else:
            text = f"文件『{name}』已在磁盘上被外部修改，是否重载？\n{target}"
        self._local_file_reload_prompting = True
        try:
            ret = QtWidgets.QMessageBox.question(
                self,
                "文件已更新",
                text,
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
        finally:
            self._local_file_reload_prompting = False
        lprint(f"[本地文件监控] 提示结果 source={source} ret={int(ret)}")
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            # 用户保留当前内容：刷新快照，避免反复提示
            self._record_loaded_local_file(target)
            self.status.showMessage(f"已保留当前内容：{name}", 2500)
            return
        self._reload_local_file_from_disk(target)

    def _visible_editor_scroll_area(self, editor):
        """返回当前可见的滚动区域。

        markdown 预览态下可见的是独立预览层（编辑区被 hide），滚动位置必须从它身上取。
        """
        is_preview = getattr(editor, "is_markdown_preview_mode", None)
        if not (callable(is_preview) and is_preview()):
            return editor
        inner = editor.editor() if hasattr(editor, "editor") else None
        preview = getattr(inner, "_preview_view", None)
        return preview if preview is not None else editor

    def _editor_scroll_ratio(self, editor) -> float:
        """当前可见滚动区域的垂直滚动比例（0.0 表示在顶部 / 无法滚动）。"""
        area = self._visible_editor_scroll_area(editor)
        if not self._qt_is_valid(area):
            return 0.0
        bar = area.verticalScrollBar() if hasattr(area, "verticalScrollBar") else None
        if bar is None:
            return 0.0
        maximum = bar.maximum()
        return 0.0 if maximum <= 0 else bar.value() / maximum

    def _restore_editor_scroll_ratio(self, editor, ratio: float) -> None:
        """按比例还原滚动位置：重载会重建 document，Qt 会把滚动条复位到顶部。"""
        if ratio <= 0.0:
            return
        area = self._visible_editor_scroll_area(editor)
        if not self._qt_is_valid(area):
            return

        def _apply() -> None:
            if not self._qt_is_valid(area):
                return
            bar = area.verticalScrollBar()
            bar.setValue(max(0, min(int(round(bar.maximum() * ratio)), bar.maximum())))

        # QPlainTextEdit 的 maximum 要等布局完成后才更新，补一次以覆盖该情况。
        _apply()
        QtCore.QTimer.singleShot(0, _apply)

    def _reload_local_file_from_disk(self, path: Path) -> None:
        """把本地文件（笔记 / 外部文件）重新读入编辑器。"""
        if not self._qt_is_valid(getattr(self, "content_edit", None)):
            return
        try:
            is_file = path.is_file()
        except OSError:
            is_file = False
        if not is_file:
            self.status.showMessage(f"文件不存在：{path.name}", 4000)
            return
        editor = self.content_edit
        mode = self._mode_from_filename(path.name)
        scroll_ratio = self._editor_scroll_ratio(editor)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            lprint(f"读取本地文件失败：{path} ({exc})")
            self.status.showMessage(f"读取本地文件失败：{exc}", 5000)
            return
        try:
            loaded = editor.load_text_file_cached(path, mode=mode)
        except Exception as exc:
            lprint(f"重载本地文件失败：{path} ({exc})")
            loaded = False
        # 预览层持有自己的源码副本，file_cached 命中缓存等情况可能不刷新可见层：
        # 用「可见文本 == 磁盘内容」做校验，不一致就强制刷一次，保证看到的是磁盘内容。
        if not loaded or editor.toPlainText() != text:
            lprint(f"[本地文件监控] 强制刷新编辑区 loaded={loaded}")
            try:
                if editor.is_markdown_preview_mode():
                    editor.show_markdown_preview(text)
                else:
                    editor.setPlainText(text)
            except Exception as exc:
                lprint(f"刷新编辑区失败：{exc}")
                self.status.showMessage(f"刷新编辑区失败：{exc}", 5000)
                return
        self.state.dirty = False
        self._invalidate_log_content_cache(str(path))
        self._record_loaded_local_file(path)
        self._set_code_editor_status_file(path)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = None
        self._set_code_editor_status_text(str(path), size_bytes)
        self._update_title()
        # 重载后的磁盘内容进版本历史（内容未变时 add_version 自动去重）：
        # 用局部 text（刚从磁盘读出的全文）而非 _get_content_text()，
        # markdown 预览态下编辑区正文不是权威来源。
        kind, ref, title = self._current_version_context()
        if kind and ref:
            self._record_version(kind, ref, title, text)
        self._sync_version_combo_on_open()
        self._restore_editor_scroll_ratio(editor, scroll_ratio)
        self.status.showMessage(f"已重载本地文件：{path.name}", 2500)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            if isinstance(event, QtGui.QWindowStateChangeEvent):
                old = event.oldState()
                if not (old & QtCore.Qt.WindowState.WindowMinimized) and (
                    self.windowState() & QtCore.Qt.WindowState.WindowMinimized
                ):
                    self._auto_save_note("窗口最小化", notify_tray=True)
        elif event.type() == QtCore.QEvent.Type.WindowDeactivate:
            self._hide_folder_hover_popup()
            QtCore.QTimer.singleShot(0, self._auto_save_on_deactivate)
        elif event.type() == QtCore.QEvent.Type.WindowActivate:
            QtCore.QTimer.singleShot(0, self._check_local_file_updated_on_foreground)
        super().changeEvent(event)

    def _auto_save_on_deactivate(self) -> None:
        if self.windowState() & QtCore.Qt.WindowState.WindowMinimized:
            return
        self._auto_save_note("窗口失去焦点", detail_ui=True)

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        # 顶层窗口（无边框外壳）的激活事件：本控件是外壳的内容子控件，收不到
        # WindowActivate，必须在外壳上过滤才能做到「切回程序即检查本地文件」。
        # 外壳的其它事件与下面的逻辑无关，直接放行。
        top = self.window()
        if top is not None and top is not self and watched is top:
            if event.type() in (
                QtCore.QEvent.Type.WindowActivate,
                QtCore.QEvent.Type.ActivationChange,
            ):
                QtCore.QTimer.singleShot(
                    0, self._check_local_file_updated_on_foreground
                )
            return False
        # 外部文件拖入：笔记树/列表均可接收资源管理器拖来的文件，归类「外部文件」分组
        drop_view = self._notes_drop_viewport()
        if (
            drop_view is not None
            and watched is drop_view
            and event.type()
            in (
                QtCore.QEvent.Type.DragEnter,
                QtCore.QEvent.Type.DragMove,
                QtCore.QEvent.Type.DragLeave,
                QtCore.QEvent.Type.Drop,
            )
        ):
            if self._handle_external_file_drop(event):
                return True
        # 问AI 输入框：回车发送，Shift+回车换行（只对 AI 标签页的输入框生效，
        # 笔记编辑器与预览层不受影响）
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.key()
            in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
            and not (event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
            and self._is_ai_tab_input(watched)
        ):
            self._ask_ai()
            return True
        # 笔记列表/树：右键按下先记下原选中项（Qt 会把当前项挪到右键那一项）
        notes_view = self._notes_view()
        if (
            notes_view is not None
            and watched is notes_view.viewport()
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.RightButton
        ):
            self._begin_right_click_guard(notes_view)
        # 中键拖拽调序（拖影 + 落点线）：仅在 notes_tree 的 viewport 上生效
        tree = getattr(self, "notes_tree", None)
        if tree is not None and watched is tree.viewport():
            if self._handle_tree_mid_drag(tree, event):
                return True
            # 右侧收藏星标：悬停反馈 + 左键点击切换
            if self._handle_tree_star_event(tree, event):
                return True
            # 折叠文件夹悬停：窗口左侧弹出内部笔记文件列表
            self._handle_folder_hover_event(tree, event)
        # 折叠文件夹浮层：进入则保持展开，离开则延迟收起
        popup = getattr(self, "_folder_hover_popup", None)
        popup_list = getattr(self, "_folder_hover_popup_list", None)
        watched_popup = popup is not None and (
            watched is popup
            or watched is popup_list
            or (popup_list is not None and watched is popup_list.viewport())
        )
        if watched_popup:
            if event.type() == QtCore.QEvent.Type.Enter:
                self._cancel_hide_folder_hover_popup()
            elif event.type() == QtCore.QEvent.Type.Leave:
                self._schedule_hide_folder_hover_popup()
        # CodeEditorWidget 已代理所有常用方法，直接使用
        text_targets = {
            self.content_edit,
            self.ai_answer_edit,
            self.log_view,
            self.content_edit.viewport(),
            self.ai_answer_edit.viewport(),
            self.log_view.viewport(),
        }
        if self.help_view is not None:
            text_targets.update(
                {
                    self.help_view,
                    self.help_view.viewport(),
                }
            )
        # 鼠标离开日志编辑区且已修改：立即自动保存并生成历史版本
        if event.type() == QtCore.QEvent.Type.Leave:
            leave_targets = {self.content_edit, self.content_edit.viewport()}
            try:
                _ed = self.content_edit.editor()
                leave_targets.update({_ed, _ed.viewport()})
            except Exception:
                pass
            if watched in leave_targets:
                self._maybe_autosave_log_on_leave()
        if watched in text_targets and event.type() == QtCore.QEvent.Type.Wheel:
            if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta:
                    step = 1 if delta > 0 else -1
                    self._set_text_font_size(self._text_font_size + step)
                    event.accept()
                return True
        # 图片粘贴：拦截 content_edit 的 Ctrl+V
        if (
            watched is self.content_edit
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.matches(QtGui.QKeySequence.StandardKey.Paste)
        ):
            clipboard = QtWidgets.QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime and mime.hasImage():
                img = QtGui.QImage(mime.imageData())
                if not img.isNull():
                    self._paste_image_to_editor(self.content_edit, img)
                    return True
            if mime and mime.hasUrls():
                handled = False
                for url in mime.urls():
                    path = url.toLocalFile()
                    if path and self._is_image_file(path):
                        self._insert_image_file_to_editor(self.content_edit, path)
                        handled = True
                if handled:
                    return True
        # 图片拖拽到 content_edit
        if watched is self.content_edit.viewport():
            if event.type() == QtCore.QEvent.Type.DragEnter:
                mime = event.mimeData()
                if mime and (mime.hasImage() or self._mime_has_image_urls(mime)):
                    event.acceptProposedAction()
                    return True
            if event.type() == QtCore.QEvent.Type.Drop:
                mime = event.mimeData()
                if mime and mime.hasImage():
                    img = QtGui.QImage(mime.imageData())
                    if not img.isNull():
                        self._paste_image_to_editor(self.content_edit, img)
                        return True
                if mime and mime.hasUrls():
                    handled = False
                    for url in mime.urls():
                        path = url.toLocalFile()
                        if path and self._is_image_file(path):
                            self._insert_image_file_to_editor(self.content_edit, path)
                            handled = True
                    if handled:
                        return True
        return super().eventFilter(watched, event)

    # ── 图片辅助方法 ──────────────────────────────────────────────

    @staticmethod
    def _is_image_file(path: str) -> bool:
        return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}

    @staticmethod
    def _mime_has_image_urls(mime) -> bool:
        if not mime or not mime.hasUrls():
            return False
        for url in mime.urls():
            p = url.toLocalFile()
            if p and Path(p).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}:
                return True
        return False

    def _images_dir(self) -> Path:
        root = self._notepad_list_dir() / "_images"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _save_image(self, img: QtGui.QImage, ext: str = "png") -> str | None:
        images_dir = self._images_dir()
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
        save_path = images_dir / name
        if not img.save(str(save_path)):
            lprint(f"图片保存失败：{save_path}")
            return None
        lprint(f"图片已保存：{save_path.name}")
        return str(save_path)

    def _paste_image_to_editor(self, editor: QtWidgets.QTextEdit, img: QtGui.QImage) -> None:
        saved = self._save_image(img)
        if not saved:
            return
        self._do_insert_image(editor, saved)

    def _insert_image_file_to_editor(self, editor: QtWidgets.QTextEdit, file_path: str) -> None:
        import shutil
        src = Path(file_path)
        if not src.is_file():
            return
        images_dir = self._images_dir()
        name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{src.suffix}"
        dest = images_dir / name
        try:
            shutil.copy2(str(src), str(dest))
        except Exception as exc:
            lprint(f"复制图片失败：{exc}")
            return
        lprint(f"图片已复制：{dest.name}")
        self._do_insert_image(editor, str(dest))

    def _do_insert_image(self, editor: QtWidgets.QTextEdit, abs_path: str) -> None:
        url = QtCore.QUrl.fromLocalFile(abs_path)
        doc = editor.document()
        doc.addResource(
            QtGui.QTextDocument.ResourceType.ImageResource,
            url,
            QtGui.QImage(abs_path),
        )
        cursor = editor.textCursor()
        img_fmt = QtGui.QTextImageFormat()
        img_fmt.setName(url.toString())
        # 限制图片最大宽度为编辑器宽度的 90%
        source_img = QtGui.QImage(abs_path)
        if not source_img.isNull():
            max_w = editor.viewport().width() * 0.9
            if source_img.width() > max_w:
                img_fmt.setWidth(max_w)
                img_fmt.setHeight(source_img.height() * max_w / source_img.width())
        cursor.insertImage(img_fmt)
        cursor.insertText("\n")
        editor.setTextCursor(cursor)
        self._mark_dirty()

    def _get_content_html(self) -> str:
        # CodeEditorWidget 已代理 toHtml 方法
        return self.content_edit.toHtml()

    def _get_content_text(self) -> str:
        return self.content_edit.toPlainText()

    def _set_content_html(self, content: str) -> None:
        # CodeEditorWidget 已代理 setHtml 和 setPlainText 方法
        if self._looks_like_html(content):
            self.content_edit.setHtml(content)
            self._reload_local_images(self.content_edit)
        else:
            self.content_edit.setPlainText(content)

    def _set_content_text(self, content: str) -> None:
        self.content_edit.setPlainText(content or "")

    @staticmethod
    def _looks_like_html(text: str) -> bool:
        s = (text or "").strip()
        return s.startswith("<!DOCTYPE") or s.startswith("<html") or "<img " in s[:2000]

    def _reload_local_images(self, editor: QtWidgets.QTextEdit) -> None:
        doc = editor.document()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    if fmt.isImageFormat():
                        img_fmt = fmt.toImageFormat()
                        name = img_fmt.name()
                        url = QtCore.QUrl(name)
                        local = url.toLocalFile() if url.isLocalFile() else name
                        if local and Path(local).is_file():
                            doc.addResource(
                                QtGui.QTextDocument.ResourceType.ImageResource,
                                url,
                                QtGui.QImage(local),
                            )
                it += 1
            block = block.next()

    def _set_text_font_size(self, size: int) -> None:
        size = max(8, min(28, int(size)))
        if size == self._text_font_size:
            return
        self._text_font_size = size
        self._apply_text_font_size()
        self._settings.setValue("ui/text_font_size", str(size))
        self._settings.sync()
        self.status.showMessage(f"日志字体大小：{size}", 1500)

    def _apply_text_font_size(self) -> None:
        font = QtGui.QFont("Cascadia Mono", self._text_font_size)
        # CodeEditorWidget 已代理 setFont 和 document 方法
        editors: list[CodeEditorWidget] = [
            self.content_edit,
            self.ai_answer_edit,
            self.log_view,
        ]
        if self.help_view is not None:
            editors.append(self.help_view)
        if self.ai_tabs is not None:
            for i in range(self.ai_tabs.count()):
                tab = self.ai_tabs.widget(i)
                if tab is None:
                    continue
                for key in ("content_edit", "ai_answer_edit"):
                    w = tab.property(key)
                    if isinstance(w, CodeEditorWidget):
                        editors.append(w)
        for editor_widget in editors:
            editor_widget.setFont(font)
            editor_widget.document().setDefaultFont(font)

    def _show_error(self, message: str) -> None:
        QtWidgets.QMessageBox.critical(self, "错误", message)

    def _update_title(self) -> None:
        suffix = " *" if self.state.dirty else ""
        if self._current_external_file:
            # 外部文件：显示完整路径层级
            p = Path(self._current_external_file)
            try:
                # 尽量显示相对 notepad_list 的相对路径
                rel = p.relative_to(self._notepad_list_dir()).as_posix()
            except Exception:
                rel = p.as_posix()
            cur = rel.replace("/", " › ")
        elif self.state.current_note_id:
            try:
                note = self.api.get_note(self.state.current_note_id)
                # note.title 为相对 notepad_list 的路径，例如 "日志A/maya_xxx.py"
                raw_title = note.title or f"#{note.id}"
                cur = raw_title.replace("/", " › ").replace("\\", " › ")
            except Exception:
                cur = f"#{self.state.current_note_id}"
        else:
            cur = "新建"
        self.setWindowTitle(f"L Notepad - {cur}{suffix}")

    @QtCore.Slot()
    def show_from_hotkey(self) -> None:
        lprint("收到快捷键触发，尝试显示到前台")
        top = self.window()
        # 恢复为正常宽度（如果之前被 Ctrl+中键 缩小到 400px）
        if hasattr(self, "_normal_width") and self._normal_width is not None:
            top.resize(self._normal_width, top.height())
            lprint(f"恢复正常宽度: {self._normal_width}px")
        self._bring_to_front()
        QtCore.QTimer.singleShot(0, self._check_local_file_updated_on_foreground)

    @QtCore.Slot(int)
    def show_folder_favorites_from_hotkey(self, caller_hwnd: int = 0) -> None:
        """Ctrl+鼠标 全局快捷键：将窗口右侧中部对齐鼠标位置，宽度设为400px，并切换到「文件夹收藏」标签。"""
        lprint("收到文件夹收藏快捷键 (Ctrl+鼠标)，对齐鼠标位置并切换到收藏标签")
        # caller_hwnd 由 local_main 在唤起本窗口之前捕获（此刻前台正是调用者），
        # 这里不要再调用 GetForegroundWindow，否则会把已被激活的本窗口识别成调用者。
        fg_hwnd = int(caller_hwnd) if caller_hwnd else 0
        if not fg_hwnd:
            try:
                fg_hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            except Exception:
                fg_hwnd = 0
        lprint(f"快捷键触发时前台窗口句柄: {fg_hwnd}")
        # 将前台窗口句柄传给收藏夹面板
        if self._folder_favorites_panel is not None and fg_hwnd:
            self._folder_favorites_panel.set_caller_hwnd(fg_hwnd)
        # 保存正常宽度，然后缩小到 400px
        top = self.window()
        if not hasattr(self, "_normal_width") or self._normal_width is None:
            self._normal_width = top.width()
        target_width = 400
        # 先把尺寸和位置全部算好并应用，最后再显示窗口，
        # 避免“先以旧尺寸/旧位置显示、再 resize/move”导致的闪烁跳动。
        cursor_pos = QtGui.QCursor.pos()
        win_height = top.height()
        top.resize(target_width, win_height)
        # 窗口右侧中部 = (x + width, y + height/2)，对齐到鼠标 => x=cursorX - width, y=cursorY - height/2
        new_x = cursor_pos.x() - target_width
        new_y = cursor_pos.y() - win_height // 2
        # 确保窗口不超出屏幕
        screen = QtWidgets.QApplication.screenAt(cursor_pos) or QtWidgets.QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            new_y = max(geo.top(), min(new_y, geo.bottom() - win_height))
            new_x = max(geo.left(), min(new_x, geo.right() - target_width))
        top.move(new_x, new_y)
        # 保存位置以便下次恢复
        self._saved_pos = QtCore.QPoint(new_x, new_y)
        lprint(f"窗口右侧中部已对齐鼠标: cursor=({cursor_pos.x()},{cursor_pos.y()}), win_pos=({new_x},{new_y})")
        # 尺寸/位置就绪后再显示，保证首帧即为最终形态
        self._bring_to_front()
        if self.tabs is None or self._folder_favorites_tab_index < 0:
            lprint("警告: 未找到文件夹收藏标签页")
            return
        self.tabs.setCurrentIndex(self._folder_favorites_tab_index)
        if self._folder_favorites_panel is not None:
            self._folder_favorites_panel.setFocus()

    def _on_folder_hotkey_button_changed(self, button: str) -> None:
        callback = getattr(self, "_folder_favorites_hotkey_callback", None)
        if callable(callback):
            callback(button)

    def _bring_to_front(self) -> None:
        # 本窗口可能已被 reparent 成外壳(L_FramelessMainWindow)的子部件，
        # 所有窗口级操作必须作用于真正的顶层窗口，否则对子部件取 winId 会把它
        # 提升成原生 topmost 子窗口，盖住兄弟控件导致点不动、不刷新。
        top = self.window()
        is_hidden = top.isHidden()
        is_minimized = bool(top.windowState() & QtCore.Qt.WindowState.WindowMinimized)
        lprint(f"_bring_to_front: hidden={is_hidden}, minimized={is_minimized}, pos={top.pos().x()},{top.pos().y()}")
        # 注意：不要在这里 move 到 _saved_pos。
        # _saved_pos 只在「Ctrl+鼠标 收藏夹」流程里设置（贴鼠标的弹窗位置），
        # 该流程会在 _bring_to_front 之后自行显式 move 定位；而普通 Ctrl 双击恢复
        # 时若 move 到这个陈旧位置，会让窗口跳到上次收藏夹的位置，而不是最小化前
        # 的位置。showNormal() 本身已能恢复最小化前的几何。
        top.show()
        top.showNormal()
        top.setWindowState(
            (top.windowState() & ~QtCore.Qt.WindowState.WindowMinimized)
            | QtCore.Qt.WindowState.WindowActive
        )
        top.raise_()
        top.activateWindow()
        lprint(f"show 后: hidden={top.isHidden()}, visible={top.isVisible()}, pos={top.pos().x()},{top.pos().y()}")
        if sys.platform != "win32":
            return
        try:
            import ctypes

            hwnd = int(top.winId())
            user32 = ctypes.windll.user32
            hwnd_topmost = -1
            hwnd_notopmost = -2
            swp_nomove = 0x0002
            swp_nosize = 0x0001
            swp_showwindow = 0x0040
            flags = swp_nomove | swp_nosize | swp_showwindow
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, hwnd_notopmost, 0, 0, 0, 0, flags)
            user32.SetForegroundWindow(hwnd)
            lprint("已调用 Windows 前台显示逻辑")
        except Exception as e:
            lprint(f"Windows 前台显示逻辑失败: {e}")

    def _help_markdown_path(self) -> Path:
        return Path(__file__).resolve().parent / "doc" / "help.md"

    def _load_help_page(self) -> None:
        """从 help.md 加载帮助页（Markdown 预览 + HTML 缓存）。"""
        if self.help_view is None:
            lprint("help_view 为 None，跳过加载")
            return
        path = self._help_markdown_path()
        fallback = "# L Notepad\n\n帮助文件未找到，请检查 `doc/help.md`。"
        if not path.exists():
            lprint(f"帮助文件不存在: {path}")
        else:
            lprint(f"加载帮助文件: {path}")
            # 读取文件内容用于诊断
            try:
                content = path.read_text(encoding="utf-8")
                lprint(f"帮助文件内容长度: {len(content)} 字符, {len(content.splitlines())} 行")
            except Exception as e:
                lprint(f"读取帮助文件失败: {e}")
        try:
            self.help_view.load_markdown_preview_file(path, fallback=fallback)
            lprint("帮助页面加载完成")
            # 检查 help_view 是否有内容
            if hasattr(self.help_view, 'toPlainText'):
                text = self.help_view.toPlainText()
                lprint(f"help_view 文本长度: {len(text)} 字符")
            if hasattr(self.help_view, 'toHtml'):
                html = self.help_view.toHtml()
                lprint(f"help_view HTML 长度: {len(html)} 字符")
        except Exception as e:
            lprint(f"帮助页面加载失败: {e}")
            import traceback
            lprint(traceback.format_exc())

    def _append_exception_log(self, title: str) -> None:
        detail = traceback.format_exc().rstrip()
        self._append_console_line(f"ERROR {title}:\n{detail}")

    def _log_unhandled_exception(self, exc_type, exc_value, exc_traceback) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)).rstrip()
        self._append_console_line(f"ERROR 未捕获异常:\n{detail}")
        if getattr(self, "_previous_excepthook", None) is not None:
            self._previous_excepthook(exc_type, exc_value, exc_traceback)

    def _sort_notes(self, notes: list[NoteDto]) -> list[NoteDto]:
        # 默认按文件夹结构排序（目录字母序 + 文件名字母序）；
        # 手动排序（self._note_order，按相对路径记录）优先生效。
        rank = {p: i for i, p in enumerate(self._note_order)}

        def key(n: NoteDto):
            manual = rank.get(n.title)
            if manual is not None:
                return (0, manual, "", "")
            folder, name = self._folder_struct_key(n.title)
            return (1, 0, folder, name)

        return sorted(notes, key=key)

    @staticmethod
    def _folder_struct_key(title: str) -> tuple[str, str]:
        p = PurePosixPath((title or "").replace("\\", "/"))
        parent = str(p.parent)
        if parent in {".", ""}:
            parent = ""
        return (parent.lower(), p.name.lower())

    @staticmethod
    def _note_title_of_tree_item(item) -> str | None:
        """返回笔记 item 的相对路径(title)，非笔记项返回 None。"""
        if item is None:
            return None
        nid = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not isinstance(nid, int):
            return None
        title = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1)
        return str(title) if title else None

    def _reorder_note_by_titles(self, src_title: str, dst_title: str, *, after: bool) -> None:
        """把 src 笔记移动到 dst 笔记的前/后，写入全局手动排序并刷新。"""
        try:
            notes = self.api.list_notes()
        except ApiError:
            return
        ordered = [n.title for n in self._sort_notes(notes)]
        if src_title not in ordered or dst_title not in ordered or src_title == dst_title:
            return
        ordered.remove(src_title)
        idx = ordered.index(dst_title)
        ordered.insert(idx + 1 if after else idx, src_title)
        self._note_order = ordered
        self._save_note_order()
        self.refresh_notes()
        self.status.showMessage("已调整排序", 1500)

    # ── 右侧收藏星标（参考 l_pyside6_uv 的 FavListWidget 鼠标逻辑）──────
    @staticmethod
    def _star_hotzone(tree, pos):
        """返回 (落点 item, 收藏 key, 是否落在右侧星标热区)。

        收藏 key：笔记为 int(id)，外部/IPC 文件为带前缀的路径字符串，其它为 None。
        """
        item = tree.itemAt(pos)
        if item is None:
            return None, None, False
        key = _NoteTreeItemDelegate._favorite_key_for_role(
            item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        )
        if key is None:
            return item, None, False
        rect = tree.visualItemRect(item)
        star = pos.x() >= rect.right() - _NoteTreeItemDelegate.STAR_HOTZONE
        return item, key, star

    def _update_tree_star_hover(self, tree, pos) -> None:
        _item, hover_key, star = self._star_hotzone(tree, pos)
        if hover_key != getattr(tree, "_fav_hover_key", None) or star != getattr(
            tree, "_fav_hover_star", False
        ):
            tree._fav_hover_key = hover_key
            tree._fav_hover_star = star
            tree.viewport().update()

    def _handle_tree_star_event(self, tree, event) -> bool:
        et = event.type()
        if et == QtCore.QEvent.Type.MouseMove:
            self._update_tree_star_hover(tree, event.position().toPoint())
            return False
        if et == QtCore.QEvent.Type.Leave:
            if getattr(tree, "_fav_hover_key", None) is not None or getattr(
                tree, "_fav_hover_star", False
            ):
                tree._fav_hover_key = None
                tree._fav_hover_star = False
                tree.viewport().update()
            return False
        if (
            et == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            _item, key, star = self._star_hotzone(tree, event.position().toPoint())
            if key is not None and star:
                self._toggle_favorite_key(key)
                return True
        return False

    # ── 折叠文件夹悬停浮层：在窗口外左侧列出内部笔记文件 ──────────────
    def _folder_hover_popup_widgets(self):
        popup = getattr(self, "_folder_hover_popup", None)
        if popup is not None:
            return popup, self._folder_hover_popup_title, self._folder_hover_popup_list
        # 以顶层窗口为 owner，确保最大化时浮层仍位于主窗口之上且不被裁剪
        owner = self.window()
        popup = QtWidgets.QFrame(
            owner,
            QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        popup.setObjectName("folderHoverPopup")
        popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        popup.setMouseTracking(True)
        popup.setStyleSheet(
            "QFrame#folderHoverPopup { background:#252526; border:1px solid #555;"
            " border-radius:6px; }"
            "QLabel#folderHoverPopupTitle { color:#89DDFF; font-weight:bold; }"
            "QListWidget#folderHoverPopupList { background:transparent; color:#E9EEF5;"
            " border:none; outline:none; }"
            "QListWidget#folderHoverPopupList::item { height:22px; padding:2px 6px; }"
            "QListWidget#folderHoverPopupList::item:hover { background:#3A3D41; }"
            "QListWidget#folderHoverPopupList::item:selected { background:#094771; }"
        )
        layout = QtWidgets.QVBoxLayout(popup)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        title = QtWidgets.QLabel("")
        title.setObjectName("folderHoverPopupTitle")
        layout.addWidget(title)
        listw = QtWidgets.QListWidget()
        listw.setObjectName("folderHoverPopupList")
        listw.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        listw.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        listw.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        listw.setMouseTracking(True)
        listw.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        listw.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        listw.setUniformItemSizes(True)
        listw.itemClicked.connect(self._on_folder_popup_item_clicked)
        listw.itemActivated.connect(self._on_folder_popup_item_clicked)
        layout.addWidget(listw)
        popup.installEventFilter(self)
        listw.installEventFilter(self)
        listw.viewport().installEventFilter(self)
        popup.hide()
        self._folder_hover_popup = popup
        self._folder_hover_popup_title = title
        self._folder_hover_popup_list = listw
        return popup, title, listw

    @staticmethod
    def _collect_folder_entries(item) -> list[tuple[str, str, tuple]]:
        """展平收集文件夹内的条目：[(显示名, 相对路径提示, 打开 key)]。

        key 形如 ("note", id) / ("external", path) / ("ipc", path)。
        子文件夹不单独成行，直接递归展平其中的笔记，避免层级嵌套。
        """
        entries: list[tuple[str, str, tuple]] = []

        def _walk(node, prefix: str) -> None:
            for i in range(node.childCount()):
                child = node.child(i)
                role = child.data(0, QtCore.Qt.ItemDataRole.UserRole)
                if isinstance(role, int) and not isinstance(role, bool):
                    title = str(child.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or "")
                    name = Path(title).name or title
                    entries.append((name, f"{prefix}{name}", ("note", int(role))))
                elif isinstance(role, str) and role.startswith(EXTERNAL_FILE_PREFIX):
                    p = role[len(EXTERNAL_FILE_PREFIX):]
                    mark, _missing = self._file_entry_marker(p)
                    entries.append((f"{mark} {Path(p).name}", p, ("external", p)))
                elif isinstance(role, str) and role.startswith(IPC_FILE_PREFIX):
                    p = role[len(IPC_FILE_PREFIX):]
                    mark, _missing = self._file_entry_marker(p)
                    entries.append((f"{mark} {Path(p).name}", p, ("ipc", p)))
                elif isinstance(role, str) and role.startswith("__folder__:"):
                    sub = str(child.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or "")
                    _walk(child, f"{prefix}{sub}/")

        _walk(item, "")
        return entries

    def _folder_popup_hide_timer(self) -> QtCore.QTimer:
        timer = getattr(self, "_folder_popup_hide_timer_obj", None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            # 从文件夹移动到弹窗需要一点时间，给一个短暂宽限期再收起
            timer.setInterval(320)
            timer.timeout.connect(self._hide_folder_hover_popup)
            self._folder_popup_hide_timer_obj = timer
        return timer

    def _schedule_hide_folder_hover_popup(self) -> None:
        self._folder_popup_hide_timer().start()

    def _cancel_hide_folder_hover_popup(self) -> None:
        timer = getattr(self, "_folder_popup_hide_timer_obj", None)
        if timer is not None:
            timer.stop()

    def _handle_folder_hover_event(self, tree, event) -> None:
        et = event.type()
        if et == QtCore.QEvent.Type.MouseMove:
            self._update_folder_hover_popup(tree, event.position().toPoint())
            return
        if et == QtCore.QEvent.Type.Leave:
            # 离开树可能是移向弹窗，延迟收起，交由弹窗 Enter 取消
            if getattr(self, "_folder_hover_item", None) is not None:
                self._schedule_hide_folder_hover_popup()
            return
        if et in (
            QtCore.QEvent.Type.Wheel,
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonDblClick,
            QtCore.QEvent.Type.Resize,
        ):
            self._hide_folder_hover_popup()

    def _update_folder_hover_popup(self, tree, pos) -> None:
        item = tree.itemAt(pos)
        role = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        is_collapsed_folder = (
            item is not None
            and isinstance(role, str)
            and role.startswith("__folder__:")
            and not item.isExpanded()
        )
        if not is_collapsed_folder:
            self._hide_folder_hover_popup()
            return
        if getattr(self, "_folder_hover_item", None) is item and getattr(
            self, "_folder_hover_popup", None
        ) is not None and self._folder_hover_popup.isVisible():
            self._cancel_hide_folder_hover_popup()
            return
        self._show_folder_hover_popup(tree, item)

    def _show_folder_hover_popup(self, tree, item) -> None:
        entries = self._collect_folder_entries(item)
        if not entries:
            self._hide_folder_hover_popup()
            return
        popup, title, listw = self._folder_hover_popup_widgets()
        folder_name = item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or ""
        title.setText(f"📁 {folder_name}  ({len(entries)})")
        listw.clear()
        for text, tooltip, key in entries:
            li = QtWidgets.QListWidgetItem(text)
            li.setToolTip(tooltip)
            li.setData(QtCore.Qt.ItemDataRole.UserRole, key)
            listw.addItem(li)
        # 尺寸：宽度按内容自适应（限幅），高度按条数（限幅）
        row_h = 22
        listw.setFixedHeight(min(360, max(row_h, len(entries) * row_h + 4)))
        width = min(320, max(160, listw.sizeHintForColumn(0) + 28))
        listw.setFixedWidth(width)
        popup.adjustSize()

        # 参考剪贴板弹窗：在光标旁摆放（左下→右下→左上→右上，屏内 clamp）
        cursor_pos = QtGui.QCursor.pos()
        x, y = self._compute_folder_popup_position(
            popup.width(), popup.height(), cursor_pos
        )
        popup.move(int(x), int(y))
        popup.show()
        # 最大化/置顶窗口场景下确保浮层在最上层可见
        try:
            popup.raise_()
        except Exception:
            pass
        self._folder_hover_item = item
        self._cancel_hide_folder_hover_popup()

    def _on_folder_popup_item_clicked(self, li: QtWidgets.QListWidgetItem) -> None:
        """点击浮层条目：在主界面打开对应笔记/文件，并收起浮层。"""
        data = li.data(QtCore.Qt.ItemDataRole.UserRole)
        self._hide_folder_hover_popup()
        if not (isinstance(data, tuple) and len(data) == 2):
            return
        kind, value = data
        # 弹窗不抢焦点，点击后把主窗口提到前台再选中
        top = self.window()
        if top is not None:
            try:
                top.raise_()
                top.activateWindow()
            except Exception:
                pass
        if kind == "note":
            self._expand_tree_ancestors(self._find_tree_item_by_note_id(int(value)))
            self._select_note_id(int(value))
        elif kind == "external":
            self._select_external_file(str(value))
        elif kind == "ipc":
            self._select_ipc_file(str(value))

    def _expand_tree_ancestors(self, item) -> None:
        """展开 item 的所有祖先文件夹，并同步持久化展开状态。

        同步持久化很重要：选中笔记时可能触发自动保存→refresh_notes 异步重建树，
        重建会按持久化状态恢复展开，若不写入 True 就会又变回折叠。
        """
        if item is None or self.notes_tree is None:
            return
        if not hasattr(self, "_tree_folder_expanded_state"):
            self._tree_folder_expanded_state = {}
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            role = parent.data(0, QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(role, str) and role.startswith("__folder__:"):
                self._tree_folder_expanded_state[role] = True
            parent = parent.parent()

    # 与剪贴板弹窗 ClipboardHistoryPopup 保持一致的光标间距
    _folder_popup_cursor_gap = 28

    def _compute_folder_popup_position(
        self, width: int, height: int, pos: QtCore.QPoint
    ) -> tuple[int, int]:
        """在光标旁摆放折叠文件夹浮层，逻辑参考剪贴板弹窗 _move_near_cursor。

        优先光标左下方（保持习惯），放不下时依次尝试右下/左上/右上；
        四向都放不下取溢出最小的方向 clamp 到屏内。
        """
        gap = self._folder_popup_cursor_gap
        candidates = [
            (pos.x() - width + 12, pos.y() + gap),         # 左下（默认）
            (pos.x() + gap, pos.y() + gap),                # 右下
            (pos.x() - width + 12, pos.y() - height - gap),  # 左上
            (pos.x() + gap, pos.y() - height - gap),       # 右上
        ]
        scr = QtWidgets.QApplication.screenAt(pos)
        geo = scr.availableGeometry() if scr is not None else None
        if geo is None:
            return int(candidates[0][0]), int(candidates[0][1])
        for x, y in candidates:
            if (
                geo.left() <= x <= geo.right() - width
                and geo.top() <= y <= geo.bottom() - height
            ):
                return int(x), int(y)

        # 四向都放不下（极小分辨率/大 DPI 缩放）：取溢出最小方向 clamp 到屏内
        def _overflow(xy) -> int:
            x, y = xy
            return (
                max(0, geo.left() - x)
                + max(0, x - (geo.right() - width))
                + max(0, geo.top() - y)
                + max(0, y - (geo.bottom() - height))
            )

        bx, by = min(candidates, key=_overflow)
        return (
            int(max(geo.left(), min(bx, geo.right() - width))),
            int(max(geo.top(), min(by, geo.bottom() - height))),
        )

    def _hide_folder_hover_popup(self) -> None:
        self._cancel_hide_folder_hover_popup()
        self._folder_hover_item = None
        popup = getattr(self, "_folder_hover_popup", None)
        if popup is not None and popup.isVisible():
            popup.hide()

    # ── 中键拖拽调序（拖影 + 落点线）────────────────────────────────
    def _notes_drop_viewport(self):
        """返回当前笔记浏览控件的 viewport（树模式用树，否则用列表）。"""
        if self._notes_tree_mode and self.notes_tree is not None:
            return self.notes_tree.viewport()
        if self.notes_list is not None:
            return self.notes_list.viewport()
        return None

    def _handle_external_file_drop(self, event) -> bool:
        """接收资源管理器拖入的文件：拖入即归类到「外部文件」分组。

        返回 True 表示已消费该事件（阻止 Qt 默认拖放处理）。
        """
        Type = QtCore.QEvent.Type
        et = event.type()
        if et == Type.DragEnter:
            if _local_files_from_mime(event.mimeData()):
                event.acceptProposedAction()
                self._show_external_drop_hint(True)
                return True
            return False
        if et == Type.DragMove:
            if _local_files_from_mime(event.mimeData()):
                event.acceptProposedAction()
                self._show_external_drop_hint(True)
                return True
            return False
        if et == Type.DragLeave:
            self._show_external_drop_hint(False)
            return False
        if et == Type.Drop:
            self._show_external_drop_hint(False)
            paths = _local_files_from_mime(event.mimeData())
            if not paths:
                return False
            self._add_dropped_external_files(paths)
            event.acceptProposedAction()
            return True
        return False

    def _show_external_drop_hint(self, visible: bool) -> None:
        """拖拽悬停提示（状态栏文案）。

        不用 QRubberBand/改样式：那会在 viewport 上产生子控件与重绘事件，
        反过来又进本窗口的 eventFilter，白白跑一遍鼠标相关逻辑。
        """
        if getattr(self, "_external_drop_hint_on", False) == visible:
            return
        self._external_drop_hint_on = visible
        if visible:
            self.status.showMessage("松开即可加入「外部文件」分组", 4000)

    def _add_dropped_external_files(self, paths: list[str]) -> None:
        """把拖入的硬盘文件加入「外部文件」分组并打开第一个。

        拖入的文件来自资源管理器（用户主动要查看/编辑的硬盘文件），
        因此归「外部文件」，而不是只由其它程序推送的「IPC 文件」。
        """
        valid: list[str] = []
        for raw in paths:
            try:
                p = Path(raw)
                is_file = p.is_file()
            except OSError:
                is_file = False
            if not is_file:
                continue
            key = str(p)
            if key not in valid:
                valid.append(key)
        if not valid:
            lprint(f"[拖入外部文件] 没有可用文件: {paths}")
            self.status.showMessage("拖入的内容里没有可用文件", 3000)
            return
        new_count = sum(1 for key in valid if key not in self._external_files)
        # 本次拖入的文件按拖入顺序置顶（最近拖入排最前）
        for key in reversed(valid):
            if key in self._external_files:
                self._external_files.remove(key)
            self._external_files.insert(0, key)
        first = self._external_files[0]
        self._current_external_file = first
        self._current_ipc_file = None
        self._save_external_files_state()
        lprint(f"[拖入外部文件] 新增={new_count} 打开={first} 全部={len(valid)}")
        # 刷新后再选中：refresh_notes 是后台线程，_populate_notes 会按
        # _current_external_file 恢复选中并载入内容
        self.refresh_notes()
        self._select_external_file(first)
        if new_count:
            extra = f" 等 {len(valid)} 个文件" if len(valid) > 1 else ""
            self.status.showMessage(
                f"已加入「外部文件」：{Path(first).name}{extra}", 3000
            )
        else:
            self.status.showMessage(f"已在「外部文件」中：{Path(first).name}", 2500)

    def _handle_tree_mid_drag(self, tree, event) -> bool:
        Type = QtCore.QEvent.Type
        et = event.type()
        if et == Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.MiddleButton:
            pos = event.position().toPoint()
            item = tree.itemAt(pos)
            title = self._note_title_of_tree_item(item)
            if title is not None:
                self._mid_drag_src_title = title
                self._mid_drag_src_item = item
                self._mid_drag_start_pos = pos
                return True
            return False
        if et == Type.MouseMove and self._mid_drag_src_title is not None and not self._mid_dragging:
            start = self._mid_drag_start_pos
            if start is not None and (event.position().toPoint() - start).manhattanLength() >= QtWidgets.QApplication.startDragDistance():
                self._start_tree_mid_drag(tree)
            return True
        if et == Type.DragEnter and event.mimeData().hasFormat(NOTE_REORDER_MIME):
            event.acceptProposedAction()
            return True
        if et == Type.DragMove and event.mimeData().hasFormat(NOTE_REORDER_MIME):
            self._update_drop_indicator(tree, event.position().toPoint())
            event.acceptProposedAction()
            return True
        if et == Type.DragLeave:
            self._hide_drop_indicator()
            return False
        if et == Type.Drop and event.mimeData().hasFormat(NOTE_REORDER_MIME):
            self._hide_drop_indicator()
            src = bytes(event.mimeData().data(NOTE_REORDER_MIME)).decode("utf-8")
            self._handle_drop(tree, event.position().toPoint(), src)
            event.acceptProposedAction()
            return True
        return False

    def _start_tree_mid_drag(self, tree) -> None:
        item = self._mid_drag_src_item
        start = self._mid_drag_start_pos
        if item is None or start is None:
            return
        rect = tree.visualItemRect(item)
        pixmap = tree.viewport().grab(rect)
        mime = QtCore.QMimeData()
        mime.setData(NOTE_REORDER_MIME, (self._mid_drag_src_title or "").encode("utf-8"))
        drag = QtGui.QDrag(tree)
        drag.setMimeData(mime)
        drag.setPixmap(pixmap)
        drag.setHotSpot(start - rect.topLeft())
        self._mid_dragging = True
        try:
            drag.exec(QtCore.Qt.DropAction.MoveAction)
        finally:
            self._mid_dragging = False
            self._mid_drag_src_title = None
            self._mid_drag_src_item = None
            self._mid_drag_start_pos = None
            self._hide_drop_indicator()

    def _update_drop_indicator(self, tree, pos) -> None:
        target = tree.itemAt(pos)
        dst_folder = self._drop_target_folder(target)
        if dst_folder is None:
            self._hide_drop_indicator()
            return
        src_folder = self._folder_of_title(self._mid_drag_src_title or "")
        rect = tree.visualItemRect(target)
        if dst_folder == src_folder:
            # 同文件夹：细线表示插入位置（仅当落在笔记项上）
            if self._note_title_of_tree_item(target) is None:
                self._hide_drop_indicator()
                return
            y = rect.bottom() if pos.y() > rect.center().y() else rect.top()
            if self._drop_indicator is None:
                self._drop_indicator = QtWidgets.QRubberBand(
                    QtWidgets.QRubberBand.Shape.Line, tree.viewport()
                )
            self._drop_indicator.setGeometry(0, max(0, y - 1), tree.viewport().width(), 2)
            self._drop_indicator.show()
            if self._drop_box is not None:
                self._drop_box.hide()
        else:
            # 跨文件夹：整行方框表示「移动到此文件夹」
            if self._drop_box is None:
                self._drop_box = QtWidgets.QRubberBand(
                    QtWidgets.QRubberBand.Shape.Rectangle, tree.viewport()
                )
            self._drop_box.setGeometry(0, rect.top(), tree.viewport().width(), rect.height())
            self._drop_box.show()
            if self._drop_indicator is not None:
                self._drop_indicator.hide()

    def _hide_drop_indicator(self) -> None:
        if self._drop_indicator is not None:
            self._drop_indicator.hide()
        if self._drop_box is not None:
            self._drop_box.hide()

    def _handle_drop(self, tree, pos, src_title: str) -> None:
        target = tree.itemAt(pos)
        dst_folder = self._drop_target_folder(target)
        if dst_folder is None:
            return
        src_folder = self._folder_of_title(src_title)
        if dst_folder == src_folder:
            # 同文件夹内：记录手动排序
            dst_title = self._note_title_of_tree_item(target)
            if dst_title is None or dst_title == src_title:
                return
            rect = tree.visualItemRect(target)
            after = pos.y() > rect.center().y()
            self._reorder_note_by_titles(src_title, dst_title, after=after)
        else:
            # 跨文件夹：直接移动文件，不记录排序
            self._move_note_to_folder(src_title, dst_folder)

    def _move_note_to_folder(self, src_title: str, dst_folder: str) -> None:
        item = self._mid_drag_src_item
        note_id = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(note_id, int):
            return
        if not hasattr(self.api, "move_note"):
            self._show_error("当前后端不支持移动文件")
            return
        try:
            moved = self.api.move_note(note_id, dst_folder)
        except ApiError as e:
            self._show_error(str(e))
            return
        # 旧路径若在手动排序中，清理掉
        if src_title in self._note_order:
            self._note_order = [p for p in self._note_order if p != src_title]
            self._save_note_order()
        self.state.current_note_id = moved.id
        self.refresh_notes()
        self.status.showMessage(f"已移动到：{dst_folder or '未归档'}", 2000)

    @staticmethod
    def _folder_of_title(title: str) -> str:
        p = PurePosixPath((title or "").replace("\\", "/"))
        parent = str(p.parent)
        return "" if parent in {".", ""} else parent

    def _drop_target_folder(self, item) -> str | None:
        """返回落点对应的目标文件夹路径（'' 表示根/未归档），非法目标返回 None。"""
        if item is None:
            return None
        role = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(role, int):
            # 笔记项 → 其所在文件夹
            return self._folder_of_title(item.data(0, QtCore.Qt.ItemDataRole.UserRole + 1) or "")
        if isinstance(role, str) and role.startswith("__folder__:"):
            fp = role[len("__folder__:"):]
            if fp == FAVORITES_GROUP_NAME:
                # 「★ 收藏」是虚拟分组，不接受拖放，避免创建同名真实目录
                return None
            return "" if fp == "未归档" else fp
        return None



    @staticmethod
    def _external_key(file_path: str) -> str:
        """外部文件收藏 key（与树/列表 item 的 UserRole 一致）。"""
        return f"{EXTERNAL_FILE_PREFIX}{file_path}"

    @staticmethod
    def _ipc_key(file_path: str) -> str:
        """IPC 文件收藏 key（与树/列表 item 的 UserRole 一致）。"""
        return f"{IPC_FILE_PREFIX}{file_path}"

    def _current_favorite_key(self):
        """返回当前编辑内容的收藏 key（笔记 int / 外部与 IPC 文件 str / 其它 None）。"""
        if self._current_ipc_file:
            return self._ipc_key(self._current_ipc_file)
        if self._current_external_file:
            return self._external_key(self._current_external_file)
        if self.state.current_note_id is not None:
            return int(self.state.current_note_id)
        return None

    def _is_favorite(self, key) -> bool:
        if key is None:
            return False
        return key in self._favorite_order

    def _update_favorite_button_label(self) -> None:
        if self._is_favorite(self._current_favorite_key()):
            self.btn_favorite.setText("取消※置顶")
        else:
            self.btn_favorite.setText("※ 置顶/收藏")

    def _load_settings(self) -> None:
        fav_raw = self._settings.value("ui/favorites", "[]")
        last_raw = self._settings.value("ui/last_note_id", "")
        model_raw = self._settings.value("ai/model", DEFAULT_SILICONFLOW_MODEL)
        ai_sessions_raw = self._settings.value("ai/sessions", "[]")
        ai_current_raw = self._settings.value("ai/current_session_id", "")
        font_size_raw = self._settings.value("ui/text_font_size", "10")
        try:
            parsed = json.loads(str(fav_raw)) if fav_raw is not None else []
            favs: list[int | str] = []
            for x in parsed or []:
                if isinstance(x, bool):
                    continue
                if isinstance(x, int):
                    favs.append(int(x))
                elif isinstance(x, str) and x.strip():
                    favs.append(x)
            # 去重并保持顺序
            seen: set = set()
            self._favorite_order = [
                x for x in favs if not (x in seen or seen.add(x))
            ]
        except Exception:
            self._favorite_order = []
        try:
            self._last_open_note_id = int(last_raw) if str(last_raw).strip() else None
        except Exception:
            self._last_open_note_id = None
        model = str(model_raw or "").strip()
        self._selected_ai_model = model or DEFAULT_SILICONFLOW_MODEL
        self._ai_sessions = {}
        self._current_ai_session_id = None
        try:
            raw_sessions = json.loads(str(ai_sessions_raw)) if ai_sessions_raw is not None else []
            if isinstance(raw_sessions, list):
                for item in raw_sessions:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("session_id", "")).strip()
                    if not sid:
                        continue
                    title = str(item.get("title", "")).strip() or f"问AI {sid[-4:]}"
                    messages = item.get("messages", [])
                    parsed_messages: list[dict[str, str]] = []
                    if isinstance(messages, list):
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            role = str(msg.get("role", "")).strip()
                            content = str(msg.get("content", "")).strip()
                            if role in {"user", "assistant"} and content:
                                parsed_messages.append({"role": role, "content": content})
                    draft_prompt = str(item.get("draft_prompt", ""))
                    if _is_ai_prompt_placeholder_body(draft_prompt):
                        draft_prompt = ""
                    self._ai_sessions[sid] = AiSession(
                        session_id=sid,
                        title=title,
                        messages=parsed_messages,
                        draft_prompt=draft_prompt,
                    )
        except Exception:
            self._ai_sessions = {}
        cur = str(ai_current_raw or "").strip()
        if cur and cur in self._ai_sessions:
            self._current_ai_session_id = cur
        try:
            self._text_font_size = max(8, min(28, int(str(font_size_raw))))
        except Exception:
            self._text_font_size = 10
        # 读取目录展开状态持久化字典
        expand_raw = self._settings.value("ui/folder_expanded", "{}")
        try:
            parsed = json.loads(str(expand_raw)) if expand_raw is not None else {}
            if isinstance(parsed, dict):
                # 确保值均为 bool
                self._tree_folder_expanded_state = {str(k): bool(v) for k, v in parsed.items()}
            else:
                self._tree_folder_expanded_state = {}
        except Exception:
            self._tree_folder_expanded_state = {}
        load_indent_display_options_from_settings(self._settings)
        self._load_note_order()

    def _note_order_file(self) -> Path:
        f = paths.note_order_file()
        # 一次性迁移旧位置（AppConfigLocation/Lugwit/l_notepad_pc 下）
        base = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppConfigLocation
        )
        legacy = Path(base or str(Path.home())) / "Lugwit" / "l_notepad_pc" / "note_order.json"
        paths.move_file(legacy, f)
        return f

    def _load_note_order(self) -> None:
        try:
            f = self._note_order_file()
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                self._note_order = [str(x) for x in data] if isinstance(data, list) else []
            else:
                self._note_order = []
        except Exception:
            self._note_order = []

    def _save_note_order(self) -> None:
        try:
            self._note_order_file().write_text(
                json.dumps(self._note_order, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            lprint(f"保存手动排序失败: {exc}")

    @QtCore.Slot()
    def _apply_indent_display_settings(self) -> None:
        """从 QSettings 重新加载 Python 缩进显示选项（设置页修改后触发）。"""
        load_indent_display_options_from_settings(self._settings)

    def _restore_ai_tabs(self) -> None:
        """恢复 AI 标签页。

        以 QSettings 中已加载的会话(self._ai_sessions)为准重建全部标签页，
        这样新建但尚未保存内容的空白 tab 也能在重启后保留。
        旧版 notepad_list/问AI*.md 文件仅在会话草稿为空时用于兜底补充输入内容。
        """
        # 升序（问AI 1、问AI 2 …）：_sorted_ai_sessions 为新→旧，这里反转为旧→新
        sessions = list(reversed(self._sorted_ai_sessions()))
        if not sessions:
            return

        for session in sessions:
            self._create_ai_tab(session)
            # 兼容旧数据：会话本身没有草稿内容时，尝试从同名 .md 读取
            if not session.draft_prompt:
                self._load_ai_session_content(session)

        # 校正当前会话指向
        if self._current_ai_session_id not in self._ai_sessions:
            self._current_ai_session_id = sessions[0].session_id

        # 切换到当前会话对应的标签页
        if self._current_ai_session_id:
            for i in range(self.ai_tabs.count()):
                widget = self.ai_tabs.widget(i)
                if widget and widget.property("session_id") == self._current_ai_session_id:
                    self.ai_tabs.setCurrentIndex(i)
                    break
        self._update_ai_tab_count()
    
    def _load_ai_session_content(self, session: AiSession) -> None:
        """从 notepad_list 目录加载问AI文件内容到会话"""
        if not self.ai_tabs:
            return
        notepad_list_dir = self._notepad_list_dir()
        file_path = notepad_list_dir / f"{session.title}.md"
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8")
                # 找到对应的标签页并设置内容
                for i in range(self.ai_tabs.count()):
                    widget = self.ai_tabs.widget(i)
                    if widget and widget.property("session_id") == session.session_id:
                        content_edit = widget.property("content_edit")
                        if content_edit:
                            content_edit.setPlainText(content)
                        break
            except Exception as exc:
                lprint(f"加载问AI文件失败 {session.title}: {exc}")

    def _save_settings(self) -> None:
        try:
            self._settings.setValue("ui/favorites", json.dumps(self._favorite_order))
            self._settings.setValue(
                "ui/last_note_id",
                "" if self._last_open_note_id is None else str(self._last_open_note_id),
            )
            self._settings.setValue("ai/model", self.model_combo.currentText().strip() or self._selected_ai_model)
            ai_sessions_data = []
            for sess in self._sorted_ai_sessions():
                ai_sessions_data.append(
                    {
                        "session_id": sess.session_id,
                        "title": sess.title,
                        "messages": sess.messages[-100:],
                        "draft_prompt": sess.draft_prompt,
                    }
                )
            self._settings.setValue("ai/sessions", json.dumps(ai_sessions_data, ensure_ascii=False))
            self._settings.setValue("ai/current_session_id", self._current_ai_session_id or "")
            self._settings.setValue("ui/text_font_size", str(self._text_font_size))
            # 保存目录展开状态
            try:
                self._settings.setValue(
                    "ui/folder_expanded",
                    json.dumps(getattr(self, "_tree_folder_expanded_state", {}) or {}),
                )
            except Exception:
                pass
            # 只在窗口可见且尺寸有效时才保存几何，避免在初始化/隐藏/关闭过程
            # 中用 (0,0,0,0) 覆盖掉之前保存的有效位置与大小（重启后无法还原）。
            try:
                if self.isVisible() and self.width() > 0 and self.height() > 0:
                    self._settings.setValue("window/geometry", self.saveGeometry())
            except Exception:
                pass
            self._settings.sync()
        except Exception:
            pass

    def _ensure_ai_session(self, session_id: str | None = None) -> AiSession:
        if session_id and session_id in self._ai_sessions:
            return self._ai_sessions[session_id]
        if self._current_ai_session_id and self._current_ai_session_id in self._ai_sessions:
            return self._ai_sessions[self._current_ai_session_id]
        return self._new_ai_session(select=True)

    def _new_ai_session(self, select: bool = True) -> AiSession:
        # 如果会话数量超过10个，删除最旧的会话
        if len(self._ai_sessions) >= 10:
            lprint("AI 会话超过上限，准备删除最旧会话")
            self._remove_oldest_ai_session()

        sid = str(QtCore.QDateTime.currentMSecsSinceEpoch())
        title = f"问AI {len(self._ai_sessions) + 1}"
        sess = AiSession(session_id=sid, title=title, messages=[])
        self._ai_sessions[sid] = sess
        if select:
            self._current_ai_session_id = sid
        lprint(f"创建 AI 会话: {sid} / {title} / select={select}")
        # 创建标签页
        self._create_ai_tab(sess)
        self._save_settings()
        return sess
    
    def _remove_oldest_ai_session(self) -> None:
        """删除最旧的AI会话"""
        if not self._ai_sessions:
            return
        # 获取第一个（最旧的）会话ID
        oldest_sid = next(iter(self._ai_sessions))
        # 删除对应的标签页
        for i in range(self.ai_tabs.count()):
            widget = self.ai_tabs.widget(i)
            if widget and widget.property("session_id") == oldest_sid:
                self.ai_tabs.removeTab(i)
                self._right_widget_refs.pop("ai_answer_edit", None)
                break
        # 删除会话
        del self._ai_sessions[oldest_sid]

    def _create_ai_tab(self, session: AiSession) -> None:
        """为 AI 会话创建一个标签页（从 .ui 模板克隆为 CodeEditorWidget）"""
        tab_widget = QtWidgets.QWidget()
        tab_layout = QtWidgets.QVBoxLayout(tab_widget)
        tab_layout.setSpacing(10)
        tab_layout.setContentsMargins(10, 10, 10, 10)

        template_content = getattr(self, "_ai_template_content_edit", None)
        template_answer = getattr(self, "_ai_template_answer_edit", None)

        content_edit = create_code_editor_widget(tab_widget, "CodeEditor", template_content)
        if template_content is None:
            content_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
            content_edit.setPlaceholderText(_AI_PROMPT_PLACEHOLDER)

        ai_answer_edit = create_code_editor_widget(tab_widget, "AiAnswerViewer", template_answer)
        if template_answer is None:
            ai_answer_edit.setReadOnly(True)
            ai_answer_edit.setPlaceholderText(_AI_ANSWER_PLACEHOLDER)

        tab_layout.addWidget(content_edit, 1)
        tab_layout.addWidget(ai_answer_edit, 2)

        font = QtGui.QFont("Cascadia Mono", self._text_font_size)
        for editor_widget in (content_edit, ai_answer_edit):
            editor_widget.setFont(font)
            editor_widget.document().setDefaultFont(font)

        tab_widget.setProperty("session_id", session.session_id)
        tab_widget.setProperty("content_edit", content_edit)
        tab_widget.setProperty("ai_answer_edit", ai_answer_edit)

        content_edit.editor().textChanged.connect(
            lambda: self._on_ai_tab_text_changed(session.session_id)
        )
        # 回车发送：输入框按键走 eventFilter（Shift+回车仍换行）
        content_edit.setToolTip("回车发送，Shift+回车换行")
        for target in (content_edit.editor(), content_edit.editor().viewport()):
            target.installEventFilter(self)

        # 添加到标签页
        index = self.ai_tabs.addTab(tab_widget, session.title)
        self.ai_tabs.setCurrentIndex(index)

        # 为新标签设置关闭按钮
        self._setup_ai_tab_close_button(index)
        self._update_ai_tab_count()
        self._update_ai_tab_content(session)

    def _update_ai_tab_count(self) -> None:
        count = self.ai_tabs.count() if self._qt_is_valid(getattr(self, "ai_tabs", None)) else 0
        if self._qt_is_valid(getattr(self, "tab_count", None)):
            self.tab_count.setText(f"Tab: {count}")
        else:
            lprint(f"tab_count 已失效，跳过显示更新: {count}")
        lprint(f"AI标签页数量: {count}")

    def _setup_ai_tab_close_button(self, index: int) -> None:
        """为指定索引的AI标签页设置关闭按钮"""
        tab_bar = self.ai_tabs.tabBar()
        if index < 0 or index >= tab_bar.count():
            return
        # 创建自定义关闭按钮（样式从 style.qss 加载）
        close_btn = QtWidgets.QPushButton("×", tab_bar)
        close_btn.setToolTip("关闭标签")
        # 连接到关闭槽
        def make_handler(idx: int):
            def handler():
                self._on_ai_tab_close_requested(idx)
            return handler
        close_btn.clicked.connect(make_handler(index))
        # 设置为标签的右按钮
        tab_bar.setTabButton(index, QtWidgets.QTabBar.ButtonPosition.RightSide, close_btn)

    def _on_ai_tab_text_changed(self, session_id: str) -> None:
        """AI 标签页文本变化时更新统计"""
        if session_id == self._current_ai_session_id:
            self._update_realtime_token_stats()

    def _get_current_ai_tab_widget(self) -> QtWidgets.QWidget | None:
        """获取当前 AI 标签页的 widget"""
        if self.ai_tabs.count() == 0:
            return None
        return self.ai_tabs.currentWidget()

    def _is_ai_tab_input(self, watched) -> bool:
        """watched 是否是当前 AI 标签页的输入框（或其 viewport）。"""
        if not self._ask_ai_mode:
            return False
        content_edit = self._get_ai_tab_content_edit()
        if content_edit is None or not self._qt_is_valid(content_edit):
            return False
        try:
            inner = content_edit.editor()
        except Exception:
            return False
        return watched is inner or watched is inner.viewport()

    def _get_ai_tab_content_edit(self, session_id: str | None = None) -> CodeEditorWidget | None:
        """获取指定会话的内容编辑器，如果不指定则获取当前标签页的"""
        if session_id is None:
            widget = self._get_current_ai_tab_widget()
            if widget:
                return widget.property("content_edit")
            return None
        # 查找指定 session_id 的标签页
        for i in range(self.ai_tabs.count()):
            widget = self.ai_tabs.widget(i)
            if widget and widget.property("session_id") == session_id:
                return widget.property("content_edit")
        return None

    def _get_ai_tab_answer_edit(self, session_id: str | None = None) -> CodeEditorWidget | None:
        """获取指定会话的回答编辑器，如果不指定则获取当前标签页的"""
        if session_id is None:
            widget = self._get_current_ai_tab_widget()
            if widget:
                return widget.property("ai_answer_edit")
            return None
        # 查找指定 session_id 的标签页
        for i in range(self.ai_tabs.count()):
            widget = self.ai_tabs.widget(i)
            if widget and widget.property("session_id") == session_id:
                return widget.property("ai_answer_edit")
        return None

    def _sorted_ai_sessions(self) -> list[AiSession]:
        return sorted(
            self._ai_sessions.values(),
            key=lambda s: int(s.session_id) if s.session_id.isdigit() else 0,
            reverse=True,
        )

    def _on_ai_tab_close_requested(self, index: int) -> None:
        """关闭 AI 标签页"""
        widget = self.ai_tabs.widget(index)
        if widget is None:
            return
        session_id = widget.property("session_id")
        if session_id and session_id in self._ai_sessions:
            del self._ai_sessions[session_id]
        self.ai_tabs.removeTab(index)
        widget.deleteLater()
        self._update_ai_tab_count()
        # 如果所有标签都关闭了，退出 AI 模式
        if self.ai_tabs.count() == 0:
            self._ask_ai_mode = False
            self._current_ai_session_id = None
            self._set_editor(None)
        self._save_settings()

    def _on_ai_tabs_context_menu(self, pos: QtCore.QPoint) -> None:
        """AI标签栏右键菜单"""
        # 获取点击位置对应的标签索引
        tab_bar = self.ai_tabs.tabBar()
        tab_index = tab_bar.tabAt(pos)

        if tab_index < 0:
            return

        # 切换到点击的标签
        self.ai_tabs.setCurrentIndex(tab_index)

        menu = QtWidgets.QMenu(self)

        # 关闭其他标签
        action_close_others = menu.addAction("关闭其他标签")
        action_close_others.triggered.connect(lambda: self._close_other_ai_tabs_except(tab_index))

        # 关闭当前标签
        action_close_current = menu.addAction("关闭当前标签")
        action_close_current.triggered.connect(lambda: self._on_ai_tab_close_requested(tab_index))

        menu.addSeparator()

        # 重命名标签
        action_rename = menu.addAction("重命名标签")
        action_rename.triggered.connect(lambda: self._rename_ai_tab(tab_index))

        # 在鼠标位置显示菜单
        global_pos = self.ai_tabs.mapToGlobal(pos)
        menu.exec(global_pos)

    def _close_other_ai_tabs_except(self, keep_index: int) -> None:
        """关闭除指定标签外的所有AI标签"""
        # 从后往前删除，避免索引变化
        for i in range(self.ai_tabs.count() - 1, -1, -1):
            if i != keep_index:
                self._on_ai_tab_close_requested(i)

    def _rename_ai_tab(self, tab_index: int) -> None:
        """重命名指定AI标签"""
        if tab_index < 0:
            return

        widget = self.ai_tabs.widget(tab_index)
        if widget is None:
            return

        session_id = widget.property("session_id")
        if not session_id or session_id not in self._ai_sessions:
            return

        session = self._ai_sessions[session_id]
        new_title, ok = QtWidgets.QInputDialog.getText(
            self, "重命名标签", "请输入新标签名:",
            QtWidgets.QLineEdit.EchoMode.Normal,
            session.title
        )
        if ok and new_title.strip():
            session.title = new_title.strip()
            self.ai_tabs.setTabText(tab_index, session.title)
            # 如果重命名的是当前标签，更新标题编辑框
            if tab_index == self.ai_tabs.currentIndex():
                self.title_edit.setText(session.title)
            self._save_settings()

    def _setup_ai_tab_close_buttons(self) -> None:
        """为所有AI标签页设置关闭按钮图标（X符号）"""
        tab_bar = self.ai_tabs.tabBar()
        for i in range(tab_bar.count()):
            # 创建自定义关闭按钮（样式从 style.qss 加载）
            close_btn = QtWidgets.QPushButton("×", tab_bar)
            close_btn.setToolTip("关闭标签")
            # 连接到关闭槽（使用闭包捕获当前索引）
            def make_close_handler(index: int):
                def handler():
                    self._on_ai_tab_close_requested(index)
                return handler
            close_btn.clicked.connect(make_close_handler(i))
            # 设置为标签的右按钮
            tab_bar.setTabButton(i, QtWidgets.QTabBar.ButtonPosition.RightSide, close_btn)

    def _on_ai_tab_changed(self, index: int) -> None:
        """切换 AI 标签页"""
        if index < 0 or not self._qt_is_valid(getattr(self, "ai_tabs", None)):
            return

        widget = self.ai_tabs.widget(index)
        if widget is None:
            return
        session_id = widget.property("session_id")
        if session_id and session_id in self._ai_sessions:
            self._current_ai_session_id = session_id
            session = self._ai_sessions[session_id]
            # 切换标签页时同步刷新当前页内容，避免右侧显示旧会话内容
            self._update_ai_tab_content(session)
            # 更新标题编辑框
            if self._qt_is_valid(getattr(self, "title_edit", None)):
                self.title_edit.blockSignals(True)
                self.title_edit.setText(session.title)
                self.title_edit.blockSignals(False)
            # 更新 token 统计
            self._update_realtime_token_stats()
            self._update_ai_ask_button_state()
            self._set_ai_status_bar(session)
        self._save_settings()

    def _render_ai_session_text(self, session: AiSession) -> str:
        chunks: list[str] = []
        if not session.messages and not session.streaming_text and not session.reasoning_text:
            return ""
        # 最后一条 assistant 消息的索引（思考过程显示在其上方）
        last_assistant = -1
        for i, msg in enumerate(session.messages):
            if msg.get("role") != "user":
                last_assistant = i
        reasoning_block = (
            f"<思考过程>\n{session.reasoning_text}\n</思考过程>\n"
            if session.reasoning_text
            else ""
        )
        reasoning_emitted = False
        for i, msg in enumerate(session.messages):
            role = "你" if msg.get("role") == "user" else "AI"
            if reasoning_block and i == last_assistant:
                chunks.append(reasoning_block)
                reasoning_emitted = True
            chunks.append(f"{role}:\n{msg.get('content', '')}\n")
        # 流式阶段（尚无最终 assistant 消息）：思考过程在输出内容上方
        if reasoning_block and not reasoning_emitted:
            chunks.append(reasoning_block)
        if session.streaming_text:
            chunks.append(f"AI(输出中):\n{session.streaming_text}")
        return "\n".join(chunks).strip()

    def _restore_window_state(self) -> None:
        geo = self._settings.value("window/geometry")
        # PySide6 里 QSettings 返回的是 QByteArray，不是 bytes/bytearray，
        # 之前用 isinstance(geo,(bytes,bytearray)) 判断恒为 False，导致窗口
        # 位置/大小永远恢复不了（重启后总是回到默认 980x640）。直接交给
        # restoreGeometry，它对无效/空数据会安全地返回 False，仅需排除 None。
        if geo is not None:
            self.restoreGeometry(geo)

