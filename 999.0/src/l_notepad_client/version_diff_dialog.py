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
import re
from dataclasses import dataclass
from typing import Callable, Hashable

from PySide6 import QtCore, QtGui, QtWidgets

from l_qt_wgt_lib.smart_widget import CodeEditorWidget
# 预览层与块渲染必须和编辑器预览用同一套实现，否则同一份 Markdown 在两处长得不一样。
# 这两个是库内的模块级实现，官方未导出公开别名，这里按私有名引用。
from l_qt_wgt_lib.smart_widget.code_editor import (
    _render_markdown_block as render_markdown_block,
)
from l_qt_wgt_lib.smart_widget.code_editor import (
    _split_markdown_blocks as split_markdown_blocks,
)
from l_qt_wgt_lib.smart_widget.code_editor import (
    build_markdown_preview_qss,
    get_markdown_preview_theme,
)

# 单侧超过该行数时只对比前 N 行，避免超长日志把界面拖死
MAX_DIFF_LINES = 20000

# 行内字符级 diff 的单行长度上限；超长的行只给整行底色（避免 SequenceMatcher 卡顿）
_MAX_INTRA_LINE_CHARS = 4000

# ── replace 区间的行配对参数 ─────────────────────────────────
# 两行相似度达到该值才认为是「同一行被改了」，否则宁可拆成删除 + 新增。
# 按偏移硬配对（i1+off ↔ j1+off）的老做法只要一侧多一行，后面整片都会错配成
# 「修改」，行内字符差异随之变成噪音——整屏黄底就是这么来的。
_PAIR_RATIO = 0.5

# 配对失败时向前找锚点的最大行数：一侧多插/删了 k 行（k ≤ 该值）都能重新对上
_ANCHOR_WINDOW = 60

# 锚点的相似度门槛比普通配对更严：锚点决定「跳过几行」，认错一次会把后面整段带偏
_ANCHOR_RATIO = 0.75

# 单个 replace 区间内允许的相似度计算次数上限。超预算后只认「完全相同」，
# 等价于退回按位配对：宁可对得粗一点，也不能让界面卡住。
_PAIR_BUDGET = 60000

# 行内字符级差异的相似度门槛：两行相似度低于此值说明是「整行重写」，
# 此时逐字标色只会把整行打成马赛克，不如只留整行底色。
_MIN_INTRA_RATIO = 0.34

# 行内差异区间的合并间距：相邻两段差异之间的「相同字符」少于该值就并成一段。
# 中文按字比较时特别需要——不合并会碎成一串一两字宽的色块。
_SPAN_MERGE_GAP = 3

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

# 预览模式的差异底色。预览页底色是 #0C1016（「终端风」主题的 page_bg），代码块自带
# 更暗的底（#0B0E14）。底色必须在这两者上都**一眼可辨**——早期版本为了「不与代码块
# 底色打架」取 #242214 / #0f1216 这类值，结果占位块几乎与页底同色、差异块也看不出，
# 用户反馈「根本不知道哪里不同」。现在按「行」上色，可见性优先。
#
# 三个差异色之间的「色相 + 明度」都要拉开：曾经三者都偏暗、又整块刷同一个色，用户
# 反馈「底色总是一模一样」——分不清新增 / 删除 / 修改，也分不清改没改。
PREVIEW_BLOCK_COLORS = {
    "delete": "#6b1f2c",  # 红：左栏删除
    "insert": "#145c33",  # 绿：右栏新增
    "replace": "#6d5314",  # 琥珀：两栏修改
    "blank": "#1b2430",  # 压暗蓝灰：对齐占位（比页底亮，才看得出「这里空着」）
}

# 改动块内的「未变行」用更淡的底色：既标出该块属于差异块，又不与真正的差异行抢眼
PREVIEW_CONTEXT_COLOR = "#141b25"

# 渲染输出的行分隔标签（mistune 的 hard_wrap 与代码高亮都用它断行）
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

# 末尾连续的收尾标签，如 ``b</span></p>`` 里的 ``</span></p>``
_TRAILING_CLOSERS_RE = re.compile(r"((?:</[A-Za-z][^>]*>)+)$")

# 任意标签的 ``名字``
_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)\b[^>]*>")

# 只允许出现在「被逐行套底色的那一行内容」里的行内标签；出现别的说明该块的渲染
# 结构与源码行不再一一对应（列表 / 表格 / 引用），必须退回整块标色
_INLINE_TAGS = frozenset(
    {
        "span",
        "a",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "s",
        "del",
        "ins",
        "code",
        "sup",
        "sub",
        "br",
    }
)

# 空行占位字符：零宽空格让 span 仍有内容可渲染（否则底色宽度为 0，看不见）
_EMPTY_LINE_CHAR = "\u200b"

# 归一化匹配用的 Markdown 标记：行首列表符 / 标题井号 / 引用符，以及行内强调符与表格竖线。
# 只删「渲染后必然消失」的标记；行内连字符等正常字符保留，否则源行与渲染文本会对不上。
_MD_MARKUP_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+|^\s*#+\s*|^\s*>\s*|[*_`~|]", re.MULTILINE)

# 归一化时的全部空白（含全角空格）
_WS_RE = re.compile(r"[\s\u3000]+")

# 归一化整块兜底匹配用：去掉 Markdown 结构标记（表格竖线、列表/标题/引用前缀、
# 强调符）与所有空白，只留正文，用来把「改动源行」映射回「渲染后的块」
_BLOCK_NORM_RE = re.compile(r"[|>#*_~`\-\s\u200b]+")


def _normalize_block_text(text: str) -> str:
    """归一化一行/一块文本用于跨源码-渲染匹配：剥掉结构标记与空白后取小写。"""
    return _BLOCK_NORM_RE.sub("", text or "").lower()

# 预览模式的块数上限：只拦病态输入，不拦正常笔记。
# 实测标定：1400 块 / 567KB 渲染 + 插入合计约 0.4s；超限时拒绝进入并给出可见原因，
# 不静默降级成「按钮没反应」（design D8）。
MAX_PREVIEW_BLOCKS = 5000

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
QPushButton#diff_mode_btn { padding: 3px 12px; border-radius: 8px; color: #8b949e; }
QPushButton#diff_mode_btn:checked {
    background: #1f6feb; border-color: #1f6feb; color: #ffffff; font-weight: 600;
}
QPushButton#diff_mode_btn:disabled { color: #6e7681; }
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
        # replace：区间内二次配对，相似的行才配成 replace，其余拆成 delete/insert
        rows.extend(_align_replace(a, b, i1, i2, j1, j2))
    return rows


def _line_ratio(x: str, y: str) -> float:
    """两行相似度 ∈ [0, 1]；空行对空行算完全相同（都是占位/空段）。"""
    if x == y:
        return 1.0
    if not x and not y:
        return 1.0
    return difflib.SequenceMatcher(None, x, y, autojunk=False).ratio()


def _align_replace(
    a: list[str], b: list[str], i1: int, i2: int, j1: int, j2: int
) -> list[DiffLine]:
    """对一个 replace 区间做行级贪心配对。

    按偏移硬配对只要一侧多一行就整片错位（后面全成 replace）。这里逐行走：
    - 当前两行够像（``ratio >= _PAIR_RATIO``）→ 配成 replace（一行改成另一行）；
    - 否则在前方一个窗口里找**强**锚点（``ratio >= _ANCHOR_RATIO``）：若右侧某行与
      当前左行高度相似，说明左侧夹了几行「纯删除」，反之则是右侧夹了「纯新增」；
      把夹层原样吐出后再让锚点行在下一轮配成 replace；
    - 找不到锚点仍按 1:1 配成 replace（并排对比里「左旧右新同行」本身就有价值，
      即使两行不相似）。只有一侧行数多出的部分，才在末尾落成纯 insert/delete。

    相似度计算有预算上限（``_PAIR_BUDGET``），超预算后只认完全相同，等价于退回
    按位配对——宁可对得粗，也不能让超大改动块把界面卡死。
    """
    left = list(range(i1, i2))
    right = list(range(j1, j2))
    budget = [_PAIR_BUDGET]

    def ratio(li: int, rj: int) -> float:
        if a[li] == b[rj]:
            return 1.0
        if budget[0] <= 0:
            return 0.0
        budget[0] -= 1
        return _line_ratio(a[li], b[rj])

    def find_anchor(li: int, rj: int) -> tuple[int, int] | None:
        """在窗口内找一对高相似行作为重新对齐点，返回 (跳过的左行数, 跳过的右行数)。"""
        best: tuple[float, int, int] | None = None
        for dr in range(len(right) - rj):
            if dr > _ANCHOR_WINDOW:
                break
            score = ratio(li, right[rj + dr])
            if score >= _ANCHOR_RATIO and (best is None or score > best[0]):
                best = (score, 0, dr)
            if score == 1.0:
                break
        for dl in range(len(left) - li):
            if dl > _ANCHOR_WINDOW:
                break
            score = ratio(left[li + dl], right[rj])
            if score >= _ANCHOR_RATIO and (best is None or score > best[0]):
                best = (score, dl, 0)
            if score == 1.0:
                break
        if best is None:
            return None
        return best[1], best[2]

    out: list[DiffLine] = []
    li = rj = 0
    while li < len(left) and rj < len(right):
        if ratio(left[li], right[rj]) >= _PAIR_RATIO:
            l, r = left[li], right[rj]
            out.append(DiffLine("replace", l + 1, r + 1, a[l], b[r]))
            li += 1
            rj += 1
            continue
        anchor = find_anchor(li, rj)
        if anchor is None:
            # 1:1 配对：并排里同行显示旧/新，即便两行不相似也比拆成两行更易读
            l, r = left[li], right[rj]
            out.append(DiffLine("replace", l + 1, r + 1, a[l], b[r]))
            li += 1
            rj += 1
            continue
        skip_left, skip_right = anchor
        for k in range(skip_left):
            l = left[li + k]
            out.append(DiffLine("delete", l + 1, None, a[l], ""))
        for k in range(skip_right):
            r = right[rj + k]
            out.append(DiffLine("insert", None, r + 1, "", b[r]))
        li += skip_left
        rj += skip_right
    while li < len(left):
        l = left[li]
        out.append(DiffLine("delete", l + 1, None, a[l], ""))
        li += 1
    while rj < len(right):
        r = right[rj]
        out.append(DiffLine("insert", None, r + 1, "", b[r]))
        rj += 1
    return out


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


def _merge_spans(spans: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """合并间距 < gap 的相邻区间：中文按字比较时差异会碎成一串窄色块，并起来更清爽。"""
    if not spans:
        return spans
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start - last_end < gap:
            merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def intra_line_spans(
    left_text: str, right_text: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """修改行的字符级差异区间（半开区间），左右各一份；超长行返回空。

    两处降噪：
    - 两行相似度低于 ``_MIN_INTRA_RATIO`` 视为整行重写，逐字标色只会打成马赛克，
      直接返回空（只保留整行底色）；
    - 相邻差异段间距 < ``_SPAN_MERGE_GAP`` 的合并成一段（中文逐字比较尤其需要）。
    """
    if max(len(left_text), len(right_text)) > _MAX_INTRA_LINE_CHARS:
        return [], []
    matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
    if matcher.ratio() < _MIN_INTRA_RATIO:
        return [], []
    left_spans: list[tuple[int, int]] = []
    right_spans: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 != i2:
            left_spans.append((i1, i2))
        if j1 != j2:
            right_spans.append((j1, j2))
    return (
        _merge_spans(left_spans, _SPAN_MERGE_GAP),
        _merge_spans(right_spans, _SPAN_MERGE_GAP),
    )


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


class _DiffPreviewView(QtWidgets.QTextEdit):
    """预览模式的渲染层：改动块也渲染，差异按行标进渲染结果里。

    为什么改动块也要渲染：对比不等于读源码。「哪一块变了」用底色能表达，但块内的
    长代码块 / 长段落如果退回源码显示，用户就看不出这版渲染出来是什么样——而那正是
    切到预览的目的。

    行级差异的落地方式：渲染输出本身是按 ``<br>`` 断行的（段落 hard_wrap、代码块
    高亮都是），所以给每一行的内容套一层行内底色 span 即可，渲染结构保持不变。
    渲染后行结构与源码行不再一一对应的块（列表 / 表格 / 引用等会被重排的结构）退回
    **整块标色**——宁可粗一点，也不为逐行着色破坏渲染结构。

    字符级差异（同一行内哪几个字变了）不在这一层做：渲染后的 HTML 里字符偏移已经
    没了，只能在源码层注入，而源码层注入会破坏跨标记的语法。字符级精度由源码模式
    提供。

    只读：对比窗不提供任何编辑入口。
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("diff_preview_view")
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QtWidgets.QTextEdit.LineWrapMode.WidgetWidth)
        font = self.font()
        font.setPointSizeF(11.0)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(12)
        # 与编辑器预览用同一套 CSS/底色，保证同一份 Markdown 在两处长得一样
        self.document().setDefaultStyleSheet(build_markdown_preview_qss())
        self.setStyleSheet(
            "QTextEdit#diff_preview_view { border: 0; background: %s; }"
            % get_markdown_preview_theme().page_bg
        )
        self._slot_block_no: list[int] = []

    # ── 渲染 ────────────────────────────────────────────────
    def render_slots(self, slots: list[BlockSlot], side: str) -> None:
        """按槽位重建整个文档：未改动块渲染，改动块也渲染并逐行标出差异。"""
        self._slot_block_no = []
        self.setUpdatesEnabled(False)
        try:
            self.clear()
            cursor = self.textCursor()
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Start)
            for index, slot in enumerate(slots):
                if index:
                    cursor.insertBlock()
                # 记「槽位起始块号」，导航时据此定位（块内容可能跨多个文档块）
                self._slot_block_no.append(cursor.blockNumber())
                own_present = (
                    slot.left_idx is not None
                    if side == "left"
                    else slot.right_idx is not None
                )
                if not own_present:
                    self._insert_placeholder(cursor, len(diff_slot_lines(slot, side)))
                    continue
                if slot.kind == "equal":
                    text = slot.left_text if side == "left" else slot.right_text
                    cursor.insertHtml(render_markdown_block(text))
                    continue
                self._insert_changed_slot(cursor, slot, side)
        finally:
            self.setUpdatesEnabled(True)
        self.verticalScrollBar().setValue(0)

    def _insert_placeholder(self, cursor: QtGui.QTextCursor, count: int) -> None:
        """对侧有内容而本侧没有：按对侧行数补空行，避免左右高度错位。"""
        for line_index in range(max(1, count)):
            if line_index:
                cursor.insertBlock()
            fmt = QtGui.QTextBlockFormat()
            fmt.setBackground(QtGui.QBrush(QtGui.QColor(PREVIEW_BLOCK_COLORS["blank"])))
            cursor.setBlockFormat(fmt)

    def _insert_changed_slot(
        self, cursor: QtGui.QTextCursor, slot: BlockSlot, side: str
    ) -> None:
        """改动槽位：照常渲染，并把行级差异标进渲染结果里。

        做法是给渲染输出的每一行套一层行内底色 span。行结构与源码对不上（列表 /
        表格 / 引用这类会被重排的块）时退回**整块标色**——宁可粗一点，也不能为了
        逐行着色把渲染结构破坏掉。
        """
        text = (slot.left_text if side == "left" else slot.right_text) or ""
        lines = diff_slot_lines(slot, side)
        kinds = render_line_kinds(text, lines)
        html = render_markdown_block(text)
        parts = split_rendered_lines(html)
        if kinds is not None and len(parts) == len(kinds):
            prefix, contents, suffix = peel_block_wrapper(parts)
            if all(has_only_inline_tags(content) for content in contents):
                if prefix:
                    cursor.insertHtml(prefix)
                for line_index, (content, kind) in enumerate(zip(contents, kinds)):
                    if line_index:
                        cursor.insertHtml("<br>")
                    # equal 行不上色：只有真正改动的行染色，未改行留原底，避免整块糊成一片
                    if kind == "equal":
                        cursor.insertHtml(content or _EMPTY_LINE_CHAR)
                    else:
                        cursor.insertHtml(wrap_line_span(content, self._line_color(kind)))
                if suffix:
                    cursor.insertHtml(suffix)
                return
        self._insert_whole_block(cursor, html, slot, side)

    @staticmethod
    def _line_color(kind: str) -> str:
        return PREVIEW_BLOCK_COLORS.get(kind, PREVIEW_CONTEXT_COLOR)

    def _insert_whole_block(
        self, cursor: QtGui.QTextCursor, html: str, slot: BlockSlot, side: str
    ) -> None:
        """整块兜底（列表 / 表格等渲染后行序被打乱的结构）：只给「文本匹配到改动源行」
        的渲染块上色，未改动的块保持原底。

        老做法是把整块刷成同一个底色——一个只改了一行的长列表会被整片染黄，用户根本
        看不出改在哪。这里按归一化文本把改动源行映射回渲染块，逐块着色。匹配不上的块
        （分隔线、结构性空块）不着色。
        """
        lines = diff_slot_lines(slot, side)
        changed: dict[str, str] = {}
        equal: list[str] = []
        for kind, text in lines:
            norm = _normalize_block_text(text)
            if not norm:
                continue
            if kind == "equal":
                equal.append(norm)
            else:
                changed[norm] = kind

        start = cursor.position()
        cursor.insertHtml(html)
        if not changed:
            return
        end = max(start, cursor.position() - 1)
        last = self.document().findBlock(end)
        block = self.document().findBlock(start)
        while block.isValid():
            norm = _normalize_block_text(block.text())
            match = next((c for c in changed if norm and norm in c), None)
            # 与未改动行也匹配上的块不染色：宁可不染，也不把没改的行误标成改动
            if match and not any(norm and norm in e for e in equal):
                color = PREVIEW_BLOCK_COLORS.get(changed[match])
                if color:
                    fmt = block.blockFormat()
                    fmt.setBackground(QtGui.QBrush(QtGui.QColor(color)))
                    QtGui.QTextCursor(block).setBlockFormat(fmt)
            if block == last:
                break
            block = block.next()

    # ── 定位 ────────────────────────────────────────────────
    def slot_block_number(self, index: int) -> int | None:
        if not (0 <= index < len(self._slot_block_no)):
            return None
        return self._slot_block_no[index]

    def scroll_to_slot(self, index: int) -> bool:
        block_no = self.slot_block_number(index)
        if block_no is None:
            return False
        block = self.document().findBlockByNumber(block_no)
        if not block.isValid():
            return False
        cursor = QtGui.QTextCursor(block)
        cursor.clearSelection()
        self.setTextCursor(cursor)
        top = self.document().documentLayout().blockBoundingRect(block).top()
        bar = self.verticalScrollBar()
        bar.setValue(
            max(0, min(int(top - self.viewport().height() / 4), bar.maximum()))
        )
        return True


@dataclass(frozen=True)
class BlockSlot:
    """块级对齐后的一个槽位；左右槽位数量始终相等，缺内容的一侧填占位块。"""

    kind: str  # equal | replace | delete | insert
    left_idx: int | None  # 该槽位对应的左侧块索引（None = 占位）
    right_idx: int | None
    left_text: str
    right_text: str


def markdown_block_texts(text: str) -> list[str]:
    """把一段 Markdown 切成块文本列表（与预览层用同一套块切分）。"""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [source[start:end] for start, end in split_markdown_blocks(source)]


def align_markdown_blocks(
    left_blocks: list[str], right_blocks: list[str]
) -> list[BlockSlot]:
    """按 Markdown 块对齐两侧，返回左右等长的槽位数组。

    行级对齐不能用来推导块级对齐：行级补位空行本身就是块边界，会让两侧的块
    边界不再可比。这里从原始文本各自切块后独立对齐。
    """
    slots: list[BlockSlot] = []
    matcher = difflib.SequenceMatcher(None, left_blocks, right_blocks, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for off in range(i2 - i1):
                slots.append(
                    BlockSlot(
                        "equal",
                        i1 + off,
                        j1 + off,
                        left_blocks[i1 + off],
                        right_blocks[j1 + off],
                    )
                )
            continue
        if tag == "delete":
            for i in range(i1, i2):
                slots.append(BlockSlot("delete", i, None, left_blocks[i], ""))
            continue
        if tag == "insert":
            for j in range(j1, j2):
                slots.append(BlockSlot("insert", None, j, "", right_blocks[j]))
            continue
        # replace：先成对当作「修改」，多出来的一侧按删除 / 新增补槽
        paired = min(i2 - i1, j2 - j1)
        for off in range(paired):
            slots.append(
                BlockSlot(
                    "replace",
                    i1 + off,
                    j1 + off,
                    left_blocks[i1 + off],
                    right_blocks[j1 + off],
                )
            )
        for i in range(i1 + paired, i2):
            slots.append(BlockSlot("delete", i, None, left_blocks[i], ""))
        for j in range(j1 + paired, j2):
            slots.append(BlockSlot("insert", None, j, "", right_blocks[j]))
    return slots


def preview_block_indices(slots: list[BlockSlot]) -> list[int]:
    """差异槽位下标（非 equal），供预览模式的差异导航。"""
    return [i for i, slot in enumerate(slots) if slot.kind != "equal"]


def diff_slot_lines(slot: BlockSlot, side: str) -> list[tuple[str, str]]:
    """把一个差异槽位摊成该侧的 ``[(行类型, 行文本)]``。

    行类型 ∈ delete / insert / replace / equal / blank。差异必须落到行上才有意义：
    块级填色只能表达「这一整块变了」，长块里到底改了哪一行仍然看不出来。

    两侧行数对齐：缺内容的一侧按对侧行数补空行，避免左右高度错位。
    """
    own_present = (
        slot.left_idx is not None if side == "left" else slot.right_idx is not None
    )
    if not own_present:
        other_lines = diff_slot_lines(slot, "right" if side == "left" else "left")
        return [("blank", "") for _ in other_lines] or [("blank", "")]

    if slot.kind in ("delete", "insert"):
        text = slot.left_text if side == "left" else slot.right_text
        return [(slot.kind, line) for line in text.split("\n")]

    # replace：块内再按行对齐，两侧行数一致（缺行补 blank）
    rows = build_side_by_side(slot.left_text, slot.right_text)
    lines: list[tuple[str, str]] = []
    for row in rows:
        own_no = row.left_no if side == "left" else row.right_no
        if own_no is None:
            lines.append(("blank", ""))
            continue
        lines.append((row.kind, row.left_text if side == "left" else row.right_text))
    return lines


def split_rendered_lines(html: str) -> list[str]:
    """按 ``<br>`` 切分渲染输出，得到「渲染行」序列（不含分隔标签）。"""
    return _BR_RE.split(html or "")


def peel_block_wrapper(parts: list[str]) -> tuple[str, list[str], str]:
    """剥出首段的起始块级标签与末段的收尾标签，返回 ``(前缀, 各行内容, 后缀)``。

    逐行套行内 span 时不能让 span 包住 ``<p>`` / ``<table>`` 这类块级标签（嵌套非法，
    Qt 会解析成别的结构），所以把外壳挪到 span 之外：
    ``<p>a<br>b</p>`` → ``<p>`` + ``[a, b]`` + ``</p>``。
    """
    contents = list(parts)
    suffix = ""
    match = _TRAILING_CLOSERS_RE.search(contents[-1])
    if match:
        suffix = match.group(1)
        contents[-1] = contents[-1][: match.start(1)]
    prefix = ""
    cut = contents[0].rfind(">")
    if cut >= 0:
        prefix = contents[0][: cut + 1]
        contents[0] = contents[0][cut + 1 :]
    return prefix, contents, suffix


def render_line_kinds(text: str, lines: list[tuple[str, str]]) -> list[str] | None:
    """按**渲染输出的行序**给出每行类型；对不上时返回 None（调用方退回整块标色）。

    围栏代码块的首尾 ``` 不会出现在渲染结果里，所以要去掉这两行；其余情况源码行与
    渲染行一一对应。
    """
    source = text.split("\n")
    if len(source) != len(lines):
        return None
    stripped = [line.strip() for line in source]
    fenced = (
        len(source) >= 2
        and stripped[0].startswith("```")
        and stripped[-1].startswith("```")
    )
    kinds = [kind for kind, _ in lines]
    if not fenced:
        return kinds
    if len(kinds) < 3:
        return None
    return kinds[1:-1]


def has_only_inline_tags(fragment: str) -> bool:
    """片段里除行内标签外不含其它标签。

    列表 / 表格 / 引用这类结构渲染后会带上 ``<li>`` / ``<td>`` / ``<p>`` 等块级标签，
    行与源码不再对应，逐行套 span 会破坏结构——这类块必须走整块标色。
    """
    for match in _TAG_RE.finditer(fragment):
        if match.group(1).lower() not in _INLINE_TAGS:
            return False
    return True


def wrap_line_span(content: str, color: str) -> str:
    """给一行渲染内容套上底色 span（空行也要占位，否则 span 无宽度看不到底色）。"""
    return f'<span style="background-color:{color};">{content or _EMPTY_LINE_CHAR}</span>'


def _normalize_block_text(text: str) -> str:
    """归一化「源行 ↔ 渲染块」匹配用的文本：去掉 Markdown 标记与全部空白。

    源码行带 ``- `` / ``| `` / ``#`` 等标记，渲染后这些标记消失（列表项还会被
    mistune 改写成 ``<li>``），直接比字符串必然对不上。只留可见字符，才能把
    「改动的源行」映射回对应的渲染块。
    """
    if not text:
        return ""
    return _WS_RE.sub("", _MD_MARKUP_RE.sub("", text))



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
        self._nav_index = -1  # 当前定位到差异集合的第几项
        self._diff_rows: list[int] = []
        # 展示模式：source = 逐行源码对比（默认，行为与既有实现一致）；
        # preview = 按 Markdown 块渲染后对比。预览态拿不到行级精度、源码态读不了
        # 渲染结果，两者解决不同问题，因此是并列模式而非替换关系。
        self._display_mode = "source"
        self._preview_supported = self._mode == "markdown"
        self._diff_blocks: list[int] = []  # 预览模式的差异槽位下标
        self._preview_slots: list[BlockSlot] = []
        self._rows: list[DiffLine] = []
        self._left_src = ""
        self._right_src = ""
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
        self._connect_preview_scrollbars()

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

        if self._preview_supported:
            bar.addWidget(self._build_mode_switcher())

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

    def _build_mode_switcher(self) -> QtWidgets.QWidget:
        """「源码 / 预览」模式切换（仅 Markdown 内容出现）。"""
        box = QtWidgets.QWidget(self)
        row = QtWidgets.QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        group = QtWidgets.QButtonGroup(box)
        group.setExclusive(True)
        self._mode_group = group
        self._mode_buttons: dict[str, QtWidgets.QPushButton] = {}
        for mode, text, tip in (
            ("source", "源码", "逐行源码对比：行号对齐、行内字符级差异"),
            ("preview", "预览", "改动块也渲染：行级差异标进渲染结果，列表/表格退回整块标色"),
        ):
            button = QtWidgets.QPushButton(text, box)
            button.setObjectName("diff_mode_btn")
            button.setCheckable(True)
            button.setChecked(mode == self._display_mode)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, m=mode: self._set_display_mode(m))
            group.addButton(button)
            self._mode_buttons[mode] = button
            row.addWidget(button)
        return box

    def _sync_mode_buttons(self) -> None:
        for mode, button in getattr(self, "_mode_buttons", {}).items():
            button.setChecked(mode == self._display_mode)

    def _set_display_mode(self, mode: str) -> None:
        """切换展示模式；两种模式的文档结构完全不同，一律整体重建。"""
        if mode == self._display_mode:
            self._sync_mode_buttons()
            return
        self._display_mode = mode
        self._nav_index = -1
        self._refresh_content(
            self._left_label, self._right_label, self._left_text, self._right_text
        )

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
        preview = _DiffPreviewView(container)
        preview.setVisible(False)
        layout.addWidget(preview, 1)
        if dot_color == "#f85149":
            self._left_preview = preview
        else:
            self._right_preview = preview
        splitter.addWidget(container)
        return container, editor

    # ── 内容刷新（初始构建 + 窗口内切换版本 + 切换模式共用）──────────
    def _refresh_content(
        self, left_label: str, right_label: str, left_text: str, right_text: str
    ) -> None:
        """重算 diff 并整体刷新：统计、标签、当前模式下的正文与导航全部重建。"""
        self._left_label = left_label
        self._right_label = right_label
        self._left_text = left_text
        self._right_text = right_text

        left_src, left_cut = _truncate_lines(left_text)
        right_src, right_cut = _truncate_lines(right_text)
        self._left_src = left_src
        self._right_src = right_src
        rows = build_side_by_side(left_src, right_src)
        self._rows = rows
        inserted, deleted, changed = diff_stats(rows)
        self._nav_index = -1

        # 统计恒按行级口径，与展示模式无关：同一个「~3 修改」在两个模式下必须
        # 指同一件事，否则用户会以为切模式后内容变了。
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
        if self._display_mode == "preview":
            notes.append("改动块按行标出差异（列表/表格整块标色）；统计仍按行")
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

        self._render_current_mode()

    def _render_current_mode(self) -> None:
        """按当前展示模式渲染；预览不可用时退回源码模式并说明原因。"""
        if self._display_mode == "preview":
            reason = self._refresh_preview_view()
            if reason is None:
                return
            # 不静默降级：退回源码的同时把原因写进提示，否则用户以为按钮坏了
            self._display_mode = "source"
            self._sync_mode_buttons()
            existing = self._note_label.text()
            self._note_label.setText(f"{existing}    {reason}" if existing else reason)
        self._refresh_source_view()

    def _refresh_source_view(self) -> None:
        """源码模式：行级对齐 + 行号栏 + 整行/行内高亮（既有实现原样保留）。"""
        rows = self._rows
        self._diff_rows = diff_row_indices(rows)
        self._diff_blocks = []
        self._show_pane("editor")

        left_lines, right_lines = build_padded_lines(rows)
        left_labels = [str(r.left_no) if r.left_no is not None else "" for r in rows]
        right_labels = [str(r.right_no) if r.right_no is not None else "" for r in rows]
        row_kinds = [r.kind for r in rows]

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

    def _show_pane(self, which: str) -> None:
        """两侧各自切换到「源码编辑器」或「预览渲染层」（两套文档结构不共用）。"""
        for edit, preview in (
            (self.left_edit, self._left_preview),
            (self.right_edit, self._right_preview),
        ):
            edit.setVisible(which == "editor")
            preview.setVisible(which == "preview")

    def _refresh_preview_view(self) -> str | None:
        """预览模式：未改动块渲染 Markdown，改动块逐行铺源码并标出差异。

        返回 None 表示成功，否则是拒绝进入预览的原因。
        """
        left_blocks = markdown_block_texts(self._left_src)
        right_blocks = markdown_block_texts(self._right_src)
        block_total = max(len(left_blocks), len(right_blocks))
        if block_total > MAX_PREVIEW_BLOCKS:
            return f"内容分块过多（{block_total} > {MAX_PREVIEW_BLOCKS}），已保持源码模式"

        slots = align_markdown_blocks(left_blocks, right_blocks)
        self._preview_slots = slots
        self._diff_blocks = preview_block_indices(slots)
        self._diff_rows = []
        self._left_preview.render_slots(slots, "left")
        self._right_preview.render_slots(slots, "right")
        self._show_pane("preview")
        self._update_nav_state()
        return None

    # ── 预览层滚动联动 ────────────────────────────────────────
    def _preview_views(self) -> list:
        return [self._left_preview, self._right_preview]

    def _connect_preview_scrollbars(self) -> None:
        """预览层两栏按比例联动（两侧块高不等，滚动条值镜像不成立）。"""
        left, right = self._preview_views()
        left_bar = left.verticalScrollBar()
        right_bar = right.verticalScrollBar()
        left_bar.valueChanged.connect(
            lambda value, s=left_bar, t=right_bar: self._mirror_scroll_ratio(s, t, value)
        )
        right_bar.valueChanged.connect(
            lambda value, s=right_bar, t=left_bar: self._mirror_scroll_ratio(s, t, value)
        )

    def _mirror_scroll_ratio(
        self, source: QtWidgets.QScrollBar, target: QtWidgets.QScrollBar, value: int
    ) -> None:
        if self._syncing:
            return
        source_max = source.maximum()
        target_max = target.maximum()
        if source_max <= 0 or target_max <= 0:
            return
        self._syncing = True
        try:
            target.setValue(int(round(value / source_max * target_max)))
        finally:
            self._syncing = False

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
    def _diff_nav_items(self) -> list[int]:
        """当前模式的差异索引集合：源码模式是行号，预览模式是块槽位下标。"""
        return self._diff_blocks if self._display_mode == "preview" else self._diff_rows

    def _goto_diff(self, step: int) -> None:
        items = self._diff_nav_items()
        if not items:
            return
        if self._nav_index < 0:
            self._nav_index = 0 if step > 0 else len(items) - 1
        else:
            self._nav_index = (self._nav_index + step) % len(items)
        if self._display_mode == "preview":
            self._goto_preview_slot(items[self._nav_index])
        else:
            self._goto_source_row(items[self._nav_index])
        self._update_nav_state()

    def _goto_source_row(self, row_no: int) -> None:
        for edit in (self.left_edit, self.right_edit):
            inner = _inner_editor(edit)
            block = inner.document().findBlockByNumber(row_no)
            if block.isValid():
                inner.setTextCursor(QtGui.QTextCursor(block))
                inner.centerCursor()
        self._reapply_selections("left")
        self._reapply_selections("right")

    def _goto_preview_slot(self, slot_index: int) -> None:
        """预览态导航：两侧各自滚到同一槽位。

        两侧块高不同，只能各自定位，不能靠滚动比例推。
        """
        for view in self._preview_views():
            view.scroll_to_slot(slot_index)

    def _update_nav_state(self) -> None:
        total = len(self._diff_nav_items())
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
