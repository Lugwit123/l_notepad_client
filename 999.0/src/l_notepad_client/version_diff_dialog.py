# -*- coding: utf-8 -*-
"""版本对比对话框（双栏并排，行对齐 + 行内高亮 + 差异导航 + 窗口内切换版本）。

- `build_side_by_side` / `diff_stats` / `build_padded_lines` / `intra_line_spans`
  / `diff_row_indices`：纯函数，行级对齐，可单测
- `VersionDiffDialog`：左右两个只读 `CodeEditorWidget`；两侧用空行补位做到
  物理行 1:1 对齐，行号栏显示各自原始行号，差异行整行着色，修改行再给
  行内差异字符加深底色，支持「上一处/下一处差异」跳转
- 传入 `versions` + `version_loader` 时，头部胶囊变为版本下拉框，
  窗口内直接切换两侧版本重新对比；不传则保持静态胶囊（只读展示）

设计见 `doc/版本对比与reload版本生成_设计.md`。
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Callable, Hashable

from PySide6 import QtCore, QtGui, QtWidgets

from l_qt_wgt_lib.smart_widget import CodeEditorWidget

# 单侧超过该行数时只对比前 N 行，避免超长日志把界面拖死
MAX_DIFF_LINES = 20000

# 行内字符级 diff 的单行长度上限；超长的行只给整行底色（避免 SequenceMatcher 卡顿）
_MAX_INTRA_LINE_CHARS = 4000

# 行背景配色（深色主题；"blank" 为对齐用的占位空行）
ROW_COLORS = {
    "delete": "#3a2026",  # 仅左栏：删除
    "insert": "#173321",  # 仅右栏：新增
    "replace": "#33301b",  # 两栏：修改
    "blank": "#0b0e13",  # 对齐补位空行
}

# 行内差异字符的加强底色（叠在整行底色之上）
SPAN_COLORS = {
    "delete": "#7a3038",
    "insert": "#276033",
    "replace": "#7a6420",
}

# 行号栏里差异行行号的前景色
GUTTER_COLORS = {
    "delete": "#f85149",
    "insert": "#3fb950",
    "replace": "#d29922",
}

# 差异导航跳转后，目标行的定位高亮色
NAV_ROW_COLOR = "#223c63"

_DIALOG_QSS = """
QDialog#VersionDiffDialog { background-color: #0d1117; }
QLabel { color: #c9d1d9; }
QLabel#diff_title { color: #e6edf3; font-size: 15px; font-weight: 600; }
QLabel#diff_chip_old {
    color: #f85149; border: 1px solid #f85149; border-radius: 8px;
    padding: 1px 10px; background: #2d161a;
}
QLabel#diff_chip_new {
    color: #3fb950; border: 1px solid #3fb950; border-radius: 8px;
    padding: 1px 10px; background: #152a1c;
}
QLabel#diff_arrow { color: #6e7681; font-weight: 600; }
QLabel#diff_note { color: #8b949e; }
QLabel#diff_counter { color: #8b949e; }
QLabel#diff_pane_title { color: #9da7b3; padding: 2px 2px; }
QComboBox#diff_version_combo { border-radius: 8px; padding: 1px 8px; font-weight: 600; }
QComboBox#diff_version_combo[side="old"] {
    color: #f85149; border: 1px solid #f85149; background: #2d161a;
}
QComboBox#diff_version_combo[side="new"] {
    color: #3fb950; border: 1px solid #3fb950; background: #152a1c;
}
QComboBox#diff_version_combo::drop-down { border: 0; width: 18px; }
QComboBox#diff_version_combo QAbstractItemView {
    background: #161b22; color: #c9d1d9;
    border: 1px solid #30363d; selection-background-color: #1f6feb;
}
QPushButton {
    background: #21262d; color: #e6edf3;
    border: 1px solid #30363d; border-radius: 6px; padding: 5px 14px;
}
QPushButton:hover { background: #30363d; border-color: #8b949e; }
QPushButton:pressed { background: #282e33; }
QPushButton:disabled { color: #6e7681; }
QPushButton#btn_diff_close {
    background: #1f6feb; border-color: #1f6feb; font-weight: 600;
}
QPushButton#btn_diff_close:hover { background: #388bfd; }
QSplitter#diff_splitter::handle { background: #30363d; width: 2px; }
"""


@dataclass(frozen=True)
class DiffLine:
    """对比结果的一行；左右两侧行号同时给出，None 表示该侧为空占位。"""

    kind: str  # equal | replace | delete | insert
    left_no: int | None
    right_no: int | None
    left_text: str
    right_text: str


def build_side_by_side(a_text: str, b_text: str) -> list[DiffLine]:
    """行级对齐两个版本，返回左右等长的行数组（同步滚动/高亮的前提）。"""
    a = (a_text or "").splitlines()
    b = (b_text or "").splitlines()
    rows: list[DiffLine] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for off in range(i2 - i1):
                rows.append(
                    DiffLine("equal", i1 + off + 1, j1 + off + 1, a[i1 + off], b[j1 + off])
                )
            continue
        if tag == "delete":
            for off in range(i2 - i1):
                rows.append(DiffLine("delete", i1 + off + 1, None, a[i1 + off], ""))
            continue
        if tag == "insert":
            for off in range(j2 - j1):
                rows.append(DiffLine("insert", None, j1 + off + 1, "", b[j1 + off]))
            continue
        # replace：逐行配对；多出的一侧用空串补位，保证左右行数一致
        for off in range(max(i2 - i1, j2 - j1)):
            has_left = off < i2 - i1
            has_right = off < j2 - j1
            rows.append(
                DiffLine(
                    "replace",
                    i1 + off + 1 if has_left else None,
                    j1 + off + 1 if has_right else None,
                    a[i1 + off] if has_left else "",
                    b[j1 + off] if has_right else "",
                )
            )
    return rows


def diff_stats(rows: list[DiffLine]) -> tuple[int, int, int]:
    """返回 (新增行, 删除行, 修改行)。"""
    inserted = deleted = changed = 0
    for row in rows:
        if row.kind == "insert":
            inserted += 1
        elif row.kind == "delete":
            deleted += 1
        elif row.kind == "replace":
            changed += 1
    return inserted, deleted, changed


def build_padded_lines(rows: list[DiffLine]) -> tuple[list[str], list[str]]:
    """把对齐后的行展开成左右两份等长文本：无内容的一侧填空串占位。

    两侧物理行 1:1 对应后，同步滚动直接按滚动条值镜像即可，不再需要比例换算。
    """
    left = [r.left_text if r.left_no is not None else "" for r in rows]
    right = [r.right_text if r.right_no is not None else "" for r in rows]
    return left, right


def intra_line_spans(
    left_text: str, right_text: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """修改行的字符级差异区间（半开区间），左右各一份；超长行返回空。"""
    if max(len(left_text), len(right_text)) > _MAX_INTRA_LINE_CHARS:
        return [], []
    matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
    left_spans: list[tuple[int, int]] = []
    right_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 != i2:
            left_spans.append((i1, i2))
        if j1 != j2:
            right_spans.append((j1, j2))
    return left_spans, right_spans


def diff_row_indices(rows: list[DiffLine]) -> list[int]:
    """所有差异行（非 equal）的物理行号（0-based），供差异导航跳转。"""
    return [i for i, row in enumerate(rows) if row.kind != "equal"]


def _truncate_lines(text: str) -> tuple[str, bool]:
    """超过上限时只保留前 MAX_DIFF_LINES 行，返回 (文本, 是否被截断)。"""
    lines = (text or "").splitlines()
    if len(lines) <= MAX_DIFF_LINES:
        return text or "", False
    return "\n".join(lines[:MAX_DIFF_LINES]), True


def _inner_editor(editor: CodeEditorWidget):
    return editor.editor() if hasattr(editor, "editor") else editor


class VersionDiffDialog(QtWidgets.QDialog):
    """双栏并排展示两个历史版本的差异（只读，不改动编辑器与版本库状态）。

    可选参数 ``versions`` + ``version_loader`` 提供时，头部胶囊变为版本下拉框，
    切换后按 loader 重新取内容并整体刷新对比；两侧允许选同一版本（提示内容一致）。
    """

    def __init__(
        self,
        *,
        name: str,
        left_label: str,
        right_label: str,
        left_text: str,
        right_text: str,
        mode: str = "text",
        dirty_hint: bool = False,
        versions: list[tuple[Hashable, str]] | None = None,
        version_loader: Callable[[Hashable], str | None] | None = None,
        left_version_id: Hashable | None = None,
        right_version_id: Hashable | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("VersionDiffDialog")
        self.setWindowTitle(f"版本对比 - {name}")
        self.resize(1280, 800)
        self.setStyleSheet(_DIALOG_QSS)
        self._mode = mode
        self._dirty_hint = dirty_hint
        self._syncing = False
        self._nav_index = -1  # 当前定位到 self._diff_rows 的第几项
        self._diff_rows: list[int] = []
        self._versions = list(versions or [])
        self._version_loader = version_loader
        self._left_version_id = left_version_id
        self._right_version_id = right_version_id
        self._switchable = bool(self._versions) and version_loader is not None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        root.addLayout(self._build_header(name))
        root.addLayout(self._build_toolbar())

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        splitter.setObjectName("diff_splitter")
        self.left_pane, self.left_edit = self._make_pane("#f85149", splitter)
        self.right_pane, self.right_edit = self._make_pane("#3fb950", splitter)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # 编辑器在光标移动时会 setExtraSelections([]) 清空高亮，这里在它之后重新铺上
        for edit, side in ((self.left_edit, "left"), (self.right_edit, "right")):
            _inner_editor(edit).cursorPositionChanged.connect(
                lambda _s=side: self._reapply_selections(_s)
            )
        self._connect_scrollbars()

        buttons = QtWidgets.QHBoxLayout()
        hint = QtWidgets.QLabel("Esc 关闭 · 左右栏已按差异对齐，滚动同步", self)
        hint.setObjectName("diff_note")
        buttons.addWidget(hint)
        buttons.addStretch()
        self.btn_close = QtWidgets.QPushButton("关闭", self)
        self.btn_close.setObjectName("btn_diff_close")
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.accept)
        buttons.addWidget(self.btn_close)
        root.addLayout(buttons)

        self._refresh_content(left_label, right_label, left_text, right_text)
        self._center_on_parent()

    # ── 界面构建 ──────────────────────────────────────────────
    def _build_header(self, name: str) -> QtWidgets.QHBoxLayout:
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        title = QtWidgets.QLabel(name, self)
        title.setObjectName("diff_title")
        header.addWidget(title)

        if self._switchable:
            self.combo_left_ver = self._make_version_combo("old", self._left_version_id)
            header.addWidget(self.combo_left_ver)
        else:
            self._left_chip = QtWidgets.QLabel(self)
            self._left_chip.setObjectName("diff_chip_old")
            header.addWidget(self._left_chip)

        arrow = QtWidgets.QLabel("→", self)
        arrow.setObjectName("diff_arrow")
        header.addWidget(arrow)

        if self._switchable:
            self.combo_right_ver = self._make_version_combo("new", self._right_version_id)
            header.addWidget(self.combo_right_ver)
        else:
            self._right_chip = QtWidgets.QLabel(self)
            self._right_chip.setObjectName("diff_chip_new")
            header.addWidget(self._right_chip)
        header.addStretch()
        return header

    def _make_version_combo(self, side: str, current_id: Hashable | None):
        """头部版本下拉框（胶囊样式）；选中变化 → 重载该侧版本内容。"""
        combo = QtWidgets.QComboBox(self)
        combo.setObjectName("diff_version_combo")
        combo.setProperty("side", side)
        combo.setMinimumWidth(240)
        combo.setToolTip("切换该侧参与对比的版本")
        for vid, label in self._versions:
            combo.addItem(str(label), vid)
        index = 0
        if current_id is not None:
            found = combo.findData(current_id)
            if found >= 0:
                index = found
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(
            lambda _i, s=side, c=combo: self._on_version_combo_changed(s, c)
        )
        return combo

    def _build_toolbar(self) -> QtWidgets.QHBoxLayout:
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(8)

        self._stats_label = QtWidgets.QLabel(self)
        self._stats_label.setObjectName("diff_stats")
        bar.addWidget(self._stats_label)

        self._note_label = QtWidgets.QLabel(self)
        self._note_label.setObjectName("diff_note")
        bar.addWidget(self._note_label)
        bar.addStretch()

        self.btn_prev_diff = QtWidgets.QPushButton("▲ 上一处", self)
        self.btn_prev_diff.setToolTip("跳到上一处差异")
        self.btn_prev_diff.clicked.connect(lambda: self._goto_diff(-1))
        bar.addWidget(self.btn_prev_diff)
        self.btn_next_diff = QtWidgets.QPushButton("▼ 下一处", self)
        self.btn_next_diff.setToolTip("跳到下一处差异")
        self.btn_next_diff.clicked.connect(lambda: self._goto_diff(1))
        bar.addWidget(self.btn_next_diff)
        self.lbl_diff_counter = QtWidgets.QLabel(self)
        self.lbl_diff_counter.setObjectName("diff_counter")
        bar.addWidget(self.lbl_diff_counter)
        return bar

    def _make_pane(
        self, dot_color: str, splitter: QtWidgets.QSplitter
    ) -> tuple[QtWidgets.QWidget, CodeEditorWidget]:
        container = QtWidgets.QWidget(splitter)
        container.setObjectName("diff_pane")
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        label = QtWidgets.QLabel(container)
        label.setObjectName("diff_pane_title")
        layout.addWidget(label)
        if dot_color == "#f85149":
            self._left_pane_title = label
        else:
            self._right_pane_title = label
        editor = CodeEditorWidget(container)
        editor.setObjectName("diff_pane_editor")
        editor.setReadOnly(True)
        editor.set_mode(self._mode)
        layout.addWidget(editor, 1)
        splitter.addWidget(container)
        return container, editor

    # ── 内容刷新（初始构建 + 窗口内切换版本共用）──────────────────
    def _refresh_content(
        self, left_label: str, right_label: str, left_text: str, right_text: str
    ) -> None:
        """重算 diff 并整体刷新：文本、行号、高亮、统计、导航全部重建。"""
        self._left_label = left_label
        self._right_label = right_label
        self._left_text = left_text
        self._right_text = right_text

        left_src, left_cut = _truncate_lines(left_text)
        right_src, right_cut = _truncate_lines(right_text)
        rows = build_side_by_side(left_src, right_src)
        inserted, deleted, changed = diff_stats(rows)
        self._diff_rows = diff_row_indices(rows)
        self._nav_index = -1
        left_lines, right_lines = build_padded_lines(rows)
        left_labels = [str(r.left_no) if r.left_no is not None else "" for r in rows]
        right_labels = [str(r.right_no) if r.right_no is not None else "" for r in rows]
        row_kinds = [r.kind for r in rows]

        self._stats_label.setText(
            f'<span style="color:#3fb950;font-weight:600;">＋{inserted} 新增</span>'
            f'&nbsp;&nbsp;<span style="color:#f85149;font-weight:600;">－{deleted} 删除</span>'
            f'&nbsp;&nbsp;<span style="color:#d29922;font-weight:600;">~{changed} 修改</span>'
        )
        notes: list[str] = []
        if inserted == deleted == changed == 0:
            notes.append("两个版本内容一致")
        if left_cut or right_cut:
            notes.append(f"内容过长，仅对比前 {MAX_DIFF_LINES} 行")
        if self._dirty_hint:
            notes.append("对比的是历史版本，不含当前未保存修改")
        self._note_label.setText("    ".join(notes))

        if self._switchable:
            pass  # 下拉框自身即版本标签
        else:
            self._left_chip.setText(f"旧 · {left_label}")
            self._left_chip.setToolTip("对比基准（左栏）")
            self._right_chip.setText(f"新 · {right_label}")
            self._right_chip.setToolTip("对比目标（右栏）")
        self._left_pane_title.setText(
            f'<span style="color:#f85149">●</span> 旧 · {left_label}'
        )
        self._right_pane_title.setText(
            f'<span style="color:#3fb950">●</span> 新 · {right_label}'
        )

        for edit, lines, labels in (
            (self.left_edit, left_lines, left_labels),
            (self.right_edit, right_lines, right_labels),
        ):
            inner = _inner_editor(edit)
            edit.setPlainText("\n".join(lines))
            # 关闭自动换行：否则长行折行后两侧物理行错位，补位对齐就白做了
            inner.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
            self._install_gutter_labels(inner, labels, row_kinds)
            inner.verticalScrollBar().setValue(0)
            inner.horizontalScrollBar().setValue(0)

        self._apply_row_highlights(self.left_edit, rows, "left")
        self._apply_row_highlights(self.right_edit, rows, "right")
        self._update_nav_state()

    def _on_version_combo_changed(self, side: str, combo: QtWidgets.QComboBox) -> None:
        """窗口内切换某侧版本：loader 取内容 → 整体重建对比。取不到则回退选择。"""
        vid = combo.currentData()
        if vid is None or self._version_loader is None:
            return
        try:
            text = self._version_loader(vid)
        except Exception:
            text = None
        if text is None:
            self._revert_version_combo(side, combo)
            return
        label = self._version_label(vid)
        if side == "old":
            self._left_version_id = vid
            self._refresh_content(label, self._right_label, text, self._right_text)
        else:
            self._right_version_id = vid
            self._refresh_content(self._left_label, label, self._left_text, text)

    def _revert_version_combo(self, side: str, combo: QtWidgets.QComboBox) -> None:
        """版本内容取不到（如已被修剪）时，把下拉框退回该侧当前版本。"""
        current = self._left_version_id if side == "old" else self._right_version_id
        index = combo.findData(current)
        if index >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _version_label(self, vid: Hashable) -> str:
        for version_id, label in self._versions:
            if version_id == vid:
                return str(label)
        return str(vid)

    # ── 行号栏：显示两侧各自的原始行号 ─────────────────────────
    def _install_gutter_labels(
        self, inner, labels: list[str], row_kinds: list[str]
    ) -> None:
        """覆盖行号栏绘制：补位空行不显示行号，差异行行号用对应强调色。"""

        def _paint(event, _inner=inner, _labels=labels, _kinds=row_kinds):
            area = _inner._line_number_area
            painter = QtGui.QPainter(area)
            painter.fillRect(event.rect(), QtGui.QColor("#080B10"))
            block = _inner.firstVisibleBlock()
            top = int(
                _inner.blockBoundingGeometry(block)
                .translated(_inner.contentOffset())
                .top()
            )
            bottom = top + int(_inner.blockBoundingRect(block).height())
            fm_height = _inner.fontMetrics().height()
            width = area.width()
            while block.isValid() and top <= event.rect().bottom():
                num = block.blockNumber()
                if block.isVisible() and bottom >= event.rect().top():
                    label = _labels[num] if num < len(_labels) else ""
                    kind = _kinds[num] if num < len(_kinds) else "equal"
                    painter.setPen(
                        QtGui.QColor(GUTTER_COLORS.get(kind, "#4B5563"))
                    )
                    painter.setFont(_inner.font())
                    painter.drawText(
                        QtCore.QRectF(0, top, width - 8, fm_height),
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignTop,
                        label,
                    )
                block = block.next()
                top = bottom
                bottom = top + int(_inner.blockBoundingRect(block).height())
            painter.end()

        inner.line_number_area_paint_event = _paint

    # ── 高亮 ─────────────────────────────────────────────────
    def _apply_row_highlights(
        self, editor: CodeEditorWidget, rows: list[DiffLine], side: str
    ) -> None:
        """整行底色（差异行 + 补位空行）+ 修改行的行内差异字符加强色。"""
        inner = _inner_editor(editor)
        document = inner.document()
        selections: list[QtWidgets.QTextEdit.ExtraSelection] = []
        for row_no, row in enumerate(rows):
            number = row.left_no if side == "left" else row.right_no
            block = document.findBlockByNumber(row_no)
            if not block.isValid():
                continue
            if row.kind == "equal":
                continue
            color = ROW_COLORS.get(row.kind) if number is not None else ROW_COLORS["blank"]
            if color is None:
                continue
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.format.setBackground(QtGui.QColor(color))
            selection.format.setProperty(
                QtGui.QTextFormat.Property.FullWidthSelection, True
            )
            cursor = QtGui.QTextCursor(block)
            cursor.clearSelection()
            selection.cursor = cursor
            selections.append(selection)

            if row.kind != "replace" or number is None:
                continue
            left_spans, right_spans = intra_line_spans(row.left_text, row.right_text)
            spans = left_spans if side == "left" else right_spans
            for start, end in spans:
                span_sel = QtWidgets.QTextEdit.ExtraSelection()
                span_sel.format.setBackground(QtGui.QColor(SPAN_COLORS["replace"]))
                span_cursor = QtGui.QTextCursor(block)
                span_cursor.setPosition(block.position() + start)
                span_cursor.setPosition(
                    block.position() + end, QtGui.QTextCursor.MoveMode.KeepAnchor
                )
                span_sel.cursor = span_cursor
                selections.append(span_sel)
        self._set_base_selections(side, selections)

    def _set_base_selections(
        self, side: str, selections: list[QtWidgets.QTextEdit.ExtraSelection]
    ) -> None:
        if not hasattr(self, "_base_selections"):
            self._base_selections = {"left": [], "right": []}
        self._base_selections[side] = selections
        self._reapply_selections(side)

    def _reapply_selections(self, side: str) -> None:
        """重铺整行/行内高亮 + 差异定位高亮（编辑器光标移动后会清空，需要补回）。"""
        edit = self.left_edit if side == "left" else self.right_edit
        inner = _inner_editor(edit)
        selections = list(getattr(self, "_base_selections", {}).get(side, []))
        nav_sel = self._nav_selection(side)
        if nav_sel is not None:
            selections.append(nav_sel)
        inner.setExtraSelections(selections)

    def _nav_selection(self, side: str) -> QtWidgets.QTextEdit.ExtraSelection | None:
        if not (0 <= self._nav_index < len(self._diff_rows)):
            return None
        edit = self.left_edit if side == "left" else self.right_edit
        block = _inner_editor(edit).document().findBlockByNumber(
            self._diff_rows[self._nav_index]
        )
        if not block.isValid():
            return None
        selection = QtWidgets.QTextEdit.ExtraSelection()
        selection.format.setBackground(QtGui.QColor(NAV_ROW_COLOR))
        selection.format.setProperty(
            QtGui.QTextFormat.Property.FullWidthSelection, True
        )
        cursor = QtGui.QTextCursor(block)
        cursor.clearSelection()
        selection.cursor = cursor
        return selection

    # ── 差异导航 ──────────────────────────────────────────────
    def _goto_diff(self, step: int) -> None:
        if not self._diff_rows:
            return
        if self._nav_index < 0:
            self._nav_index = 0 if step > 0 else len(self._diff_rows) - 1
        else:
            self._nav_index = (self._nav_index + step) % len(self._diff_rows)
        row_no = self._diff_rows[self._nav_index]
        for edit in (self.left_edit, self.right_edit):
            inner = _inner_editor(edit)
            block = inner.document().findBlockByNumber(row_no)
            if block.isValid():
                inner.setTextCursor(QtGui.QTextCursor(block))
                inner.centerCursor()
        self._update_nav_state()
        self._reapply_selections("left")
        self._reapply_selections("right")

    def _update_nav_state(self) -> None:
        total = len(self._diff_rows)
        self.lbl_diff_counter.setText(
            f"差异 {self._nav_index + 1}/{total}" if total else "无差异"
        )
        self.btn_prev_diff.setEnabled(total > 0)
        self.btn_next_diff.setEnabled(total > 0)

    # ── 滚动同步（两侧行数一致，直接镜像滚动条值）────────────────
    def _connect_scrollbars(self) -> None:
        for axis in ("vertical", "horizontal"):
            left = getattr(self.left_edit, f"{axis}ScrollBar")()
            right = getattr(self.right_edit, f"{axis}ScrollBar")()
            if left is None or right is None:
                continue
            left.valueChanged.connect(
                lambda value, s=left, t=right: self._mirror_scroll(s, t, value)
            )
            right.valueChanged.connect(
                lambda value, s=right, t=left: self._mirror_scroll(s, t, value)
            )

    def _mirror_scroll(
        self, source: QtWidgets.QScrollBar, target: QtWidgets.QScrollBar, value: int
    ) -> None:
        """镜像滚动条值（用 _syncing 阻断回调递归）。"""
        if self._syncing:
            return
        self._syncing = True
        try:
            target.setValue(value)
        finally:
            self._syncing = False

    def _center_on_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        try:
            geometry = parent.window().geometry()
            self.move(
                geometry.center().x() - self.width() // 2,
                geometry.center().y() - self.height() // 2,
            )
        except Exception:
            pass
