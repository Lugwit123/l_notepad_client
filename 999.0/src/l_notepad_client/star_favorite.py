# -*- coding: utf-8 -*-
"""收藏星标的共享绘制/命中逻辑。

主界面笔记树（ui.py 的 _NoteTreeItemDelegate）与剪贴板列表
（ClipboardItemDelegate）都用「右侧星标」表达收藏态，交互一致：

- 已收藏：始终显示实心 ★
- 未收藏：仅当鼠标悬停该行时显示空心 ☆
- 鼠标移到右侧星标热区：星标在 ☆/★ 间反转，作为可点击反馈

两处共用这里的一份实现，避免星标字符/热区宽度/绘制偏移各自漂移。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui

#: 星标可点击热区宽度（从行矩形右侧边缘往左）
STAR_HOTZONE = 26

#: 星标颜色
STAR_COLOR = "#FFB800"

#: 星标字号
STAR_FONT_POINT_SIZE = 14

#: 星标相对行右缘的内缩
STAR_RIGHT_MARGIN = 8

_FILLED = "\u2605"   # ★
_HOLLOW = "\u2606"   # ☆


def favorite_star_char(favorite: bool, star_hover: bool) -> str:
    """返回要绘制的星标字符；``star_hover`` 表示鼠标落在星标热区（反转填充态）。"""
    if star_hover:
        return _HOLLOW if favorite else _FILLED
    return _FILLED if favorite else _HOLLOW


def row_star_state(view, key, favorite: bool) -> tuple[bool, str | None]:
    """返回 ``(是否已收藏, 需绘制的星标字符)``；``None`` 表示本行无需绘制星标。

    悬停态由 ``view`` 上的 ``_fav_hover_key`` / ``_fav_hover_star`` 表达
    （由界面的悬停控制器写入）。
    """
    hovered = bool(
        key is not None and getattr(view, "_fav_hover_key", None) == key
    )
    if not favorite and not hovered:
        return favorite, None
    star_over = hovered and bool(getattr(view, "_fav_hover_star", False))
    return favorite, favorite_star_char(favorite, star_over)


def is_star_hotzone(rect: QtCore.QRect, x: int) -> bool:
    """判断行内横坐标 ``x`` 是否落在右侧星标热区。"""
    return x >= rect.right() - STAR_HOTZONE


def visible_row_rect(rect: QtCore.QRect, view) -> QtCore.QRect:
    """把行矩形裁到 ``view`` 可见的 viewport 范围。

    item 矩形可能比可见区域**宽**：QListView 在滚动条出现前完成布局时，item 宽度
    取的是当时的 viewport 宽（不含滚动条），之后滚动条占位不会立刻重排 —— 于是
    ``visualRect()`` 的右缘会落到滚动条下面十几像素。若照它右对齐，星标（以及文字
    避让宽度）就白留了，看起来"贴着滚动条"。裁一刀后，右缘恒等于真正可见的边缘，
    右侧留白才是留白；绘制与点击热区必须用同一个裁剪结果，否则会点不到星标。
    """
    if view is None:
        return rect
    viewport = view.viewport()
    if viewport is None:
        return rect
    return rect.intersected(viewport.rect())


def row_star_state_by_row(view, row: int, favorite: bool) -> tuple[bool, str | None]:
    """``row_star_state`` 的行号版：适用于条目按行号寻址的场景（剪贴板列表）。

    悬停态由 ``view`` 上的 ``_fav_hover_row`` / ``_fav_hover_star`` 表达。
    """
    hovered = getattr(view, "_fav_hover_row", -1) == row
    if not favorite and not hovered:
        return favorite, None
    star_over = hovered and bool(getattr(view, "_fav_hover_star", False))
    return favorite, favorite_star_char(favorite, star_over)


def draw_favorite_star(
    painter: QtGui.QPainter,
    rect: QtCore.QRect,
    star: str,
    font: QtGui.QFont,
    *,
    point_size: int = STAR_FONT_POINT_SIZE,
    right_margin: int = STAR_RIGHT_MARGIN,
    color: str = STAR_COLOR,
) -> None:
    """在行矩形右缘绘制星标字符（调用方负责 painter 的 save/restore）。"""
    painter.save()
    star_font = QtGui.QFont(font)
    star_font.setPointSize(point_size)
    painter.setFont(star_font)
    painter.setPen(QtGui.QPen(QtGui.QColor(color)))
    painter.drawText(
        rect.adjusted(0, 0, -right_margin, 0),
        QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
        star,
    )
    painter.restore()
