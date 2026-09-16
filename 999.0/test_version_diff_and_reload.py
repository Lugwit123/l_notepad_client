# -*- coding: utf-8 -*-
"""验证：版本对比（双栏 diff）+ 外部文件重载后写入版本历史。

配套设计：`doc/版本对比与reload版本生成_设计.md`

运行：``wuwor l_notepad_client -- python -u test_version_diff_and_reload.py``（在 999.0 目录下）
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import history_store, ui as ui_mod
from l_notepad_client.version_diff_dialog import (
    MAX_PREVIEW_BLOCKS,
    PREVIEW_BLOCK_COLORS,
    PREVIEW_CONTEXT_COLOR,
    SPAN_COLORS,
    VersionDiffDialog,
    align_markdown_blocks,
    build_padded_lines,
    build_side_by_side,
    diff_row_indices,
    diff_slot_lines,
    diff_stats,
    has_only_inline_tags,
    intra_line_spans,
    markdown_block_texts,
    peel_block_wrapper,
    preview_block_indices,
    render_line_kinds,
    wrap_line_span,
)

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


class _StatusStub:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)


class _EditorStub:
    """只实现重载流程用到的编辑器接口。"""

    def __init__(self):
        self._text = ""

    def load_text_file_cached(self, path, mode=None):
        self._text = Path(path).read_text(encoding="utf-8", errors="replace")
        return True

    def toPlainText(self):
        return self._text

    def setPlainText(self, text):
        self._text = text

    def is_markdown_preview_mode(self):
        return False

    def show_markdown_preview(self, text):
        self._text = text


# ── 1. 行级对齐与统计（纯函数）────────────────────────────────
rows = build_side_by_side("line1\nline2\nline3\nline4", "line1\nlineX\nline3\nnew\nline4")
check("replace 行配对", rows[1].kind == "replace" and rows[1].left_text == "line2")
check("insert 行左侧占位", rows[3].kind == "insert" and rows[3].left_no is None)
check("统计 新增/删除/修改", diff_stats(rows) == (1, 0, 1))

rows_del = build_side_by_side("a\nb\nc\nd", "a\nd")
check("delete 统计", diff_stats(rows_del) == (0, 2, 0))

rows_uneven = build_side_by_side("a\nb\nc", "a\nx\ny\nz\nc")
check(
    "replace 行数不等时右侧占位补齐",
    len(rows_uneven) == 5 and len([r for r in rows_uneven if r.left_no is None]) == 2,
)
check("相同内容无差异", diff_stats(build_side_by_side("same", "same")) == (0, 0, 0))

# replace 区间：不相似的 1:1 仍配成 replace（并排同行看旧/新），不硬拆成删+插
rows_pair = build_side_by_side("aaa\nccc", "aaa\nbbb")
check(
    "不相似的 1:1 行仍配 replace",
    diff_stats(rows_pair) == (0, 0, 1) and diff_row_indices(rows_pair) == [1],
    str([(r.kind, r.left_text, r.right_text) for r in rows_pair]),
)

# replace 区间里一侧夹了行：靠相似锚点重新对齐，夹层落成纯 insert（不整片错配）
rows_anchor = build_side_by_side(
    "head\nalpha line\nbeta line\ntail", "head\nalpha line changed\nINSERTED\nbeta line\ntail"
)
check(
    "夹行靠锚点重对齐：只 1 改 1 增，尾行仍 equal",
    diff_stats(rows_anchor) == (1, 0, 1)
    and rows_anchor[-1].kind == "equal"
    and any(r.kind == "insert" and r.right_text == "INSERTED" for r in rows_anchor),
    str([(r.kind, r.left_text, r.right_text) for r in rows_anchor]),
)

# ── 1b. 补位对齐 / 行内差异 / 差异行号（纯函数）────────────────
pad_left, pad_right = build_padded_lines(rows)
check("补位后左右等长", len(pad_left) == len(pad_right) == len(rows))
check("insert 行左栏补空串", pad_left[3] == "" and pad_right[3] == "new")
check("equal 行两侧都有内容", pad_left[0] == "line1" and pad_right[0] == "line1")

l_spans, r_spans = intra_line_spans("abc123def", "abc456def")
check("行内差异左右各一段", l_spans == [(3, 6)] and r_spans == [(3, 6)])
check("行内相同无差异区间", intra_line_spans("same", "same") == ([], []))
check(
    "超长行跳过行内 diff",
    intra_line_spans("x" * 5000, "y" * 5000) == ([], []),
)
# 整行重写（相似度过低）不做逐字标色，避免打成马赛克
check("整行重写不逐字标色", intra_line_spans("ccc", "bbb") == ([], []))
# 相邻窄差异段合并（逐字比较时易碎成一串小色块）：a1b2c → aXbYc 本是两段，合并成一段
l_merge, r_merge = intra_line_spans("a1b2c", "aXbYc")
check("相邻窄差异段合并成一段", l_merge == [(1, 4)] and r_merge == [(1, 4)], str((l_merge, r_merge)))
check("差异行号=非 equal 行", diff_row_indices(rows) == [1, 3])

# ── 1c. 块级对齐与差异行摊平（纯函数）──────────────────────────
md_left = "# 标题\n\n第一段\n\n- a\n- b\n\n```py\nprint(1)\n\nprint(2)\n```\n\n尾部"
md_right = "# 标题\n\n第一段改了\n\n- a\n- b\n\n```py\nprint(1)\n\nprint(2)\n```\n\n尾部\n\n新块"

blocks_left = markdown_block_texts(md_left)
blocks_right = markdown_block_texts(md_right)
check(
    "按空行切块（围栏内空行不切碎）",
    len(blocks_left) == 5 and blocks_left[3] == "```py\nprint(1)\n\nprint(2)\n```",
    str(blocks_left),
)

slots = align_markdown_blocks(blocks_left, blocks_right)
check("块级对齐槽位数=两侧块数上限", len(slots) == 6, str([s.kind for s in slots]))
check("块内改动按整块 replace", slots[1].kind == "replace" and slots[1].left_idx == 1)
check("一侧多出的块按 insert 占位", slots[5].kind == "insert" and slots[5].left_idx is None)
check("差异槽位下标", preview_block_indices(slots) == [1, 5])

# 差异块摊平成「行」：行级类型——这是「看得出哪里不同」的关键
check(
    "replace 槽位左侧行类型",
    diff_slot_lines(slots[1], "left") == [("replace", "第一段")],
    str(diff_slot_lines(slots[1], "left")),
)
check(
    "replace 槽位右侧行类型",
    diff_slot_lines(slots[1], "right") == [("replace", "第一段改了")],
    str(diff_slot_lines(slots[1], "right")),
)
check(
    "replace 槽位两侧行数一致",
    len(diff_slot_lines(slots[1], "left")) == len(diff_slot_lines(slots[1], "right")),
)
insert_slot_lines = diff_slot_lines(slots[5], "left")
check(
    "占位侧按对侧行数补空行（避免左右高度错位）",
    all(kind == "blank" and not text for kind, text in insert_slot_lines)
    and len(insert_slot_lines) == len(diff_slot_lines(slots[5], "right")),
    str(insert_slot_lines),
)
check(
    "纯新增块右侧全部为 insert 行",
    {kind for kind, _t in diff_slot_lines(slots[5], "right")} == {"insert"},
)
check(
    "纯删除块左侧全部为 delete 行",
    {kind for kind, _t in diff_slot_lines(align_markdown_blocks(["a\nb"], [])[0], "left")}
    == {"delete"},
)

# 渲染行与源码行的对应：平坦块一一对应；围栏代码块要去掉首尾 ```
check(
    "平坦块渲染行类型与源码行一致",
    render_line_kinds("a\nb", [("equal", "a"), ("replace", "b")]) == ["equal", "replace"],
)
check(
    "围栏代码块去掉首尾 ``` 后与渲染行一致",
    render_line_kinds(
        "```py\nx = 1\ny = 2\n```",
        [("equal", "```py"), ("replace", "x = 1"), ("insert", "y = 2"), ("equal", "```")],
    )
    == ["replace", "insert"],
)
check(
    "渲染行数与源码行数对不上时返回 None（退回整块标色）",
    render_line_kinds("a\nb", [("equal", "a")]) is None,
)

# 逐行套 span 的前置条件：只有行内标签才允许出现
check("纯文本行可逐行套底色", has_only_inline_tags("普通文本"))
check(
    "含行内标签的行可逐行套底色",
    has_only_inline_tags('<span style="color:#fff">粗</span><a href="x">链</a>'),
)
check(
    "含块级标签的行必须退回整块标色",
    not has_only_inline_tags("<td>x</td>")
    and not has_only_inline_tags("<li>x</li>")
    and not has_only_inline_tags("<p>x</p>"),
)

# 外壳剥离：span 不能包住块级标签，否则嵌套非法
prefix, contents, suffix = peel_block_wrapper(["<p>a", "b</p>"])
check("剥离段落的块级外壳", (prefix, contents, suffix) == ("<p>", ["a", "b"], "</p>"))
prefix, contents, suffix = peel_block_wrapper(["<h2>x</h2>"])
check("单行标题也能剥离外壳", (prefix, contents, suffix) == ("<h2>", ["x"], "</h2>"))
prefix, contents, suffix = peel_block_wrapper(
    ['<table><tr><td bgcolor="#000">a', "b</td></tr></table>"]
)
check(
    "代码块外壳（table/td）整体剥出",
    (prefix, contents, suffix)
    == ('<table><tr><td bgcolor="#000">', ["a", "b"], "</td></tr></table>"),
    str((prefix, contents, suffix)),
)
check(
    "空行用零宽空格占位（否则底色宽度为 0）",
    "\u200b" in wrap_line_span("", "#123456"),
)

check("两侧全等时无差异槽位", preview_block_indices(align_markdown_blocks(["a", "b"], ["a", "b"])) == [])
check("一侧为空时全部为 delete", [s.kind for s in align_markdown_blocks(["a"], [])] == ["delete"])
check("两侧都为空时无槽位", align_markdown_blocks([], []) == [])
check(
    "首块差异（replace 后逐块配对）",
    [s.kind for s in align_markdown_blocks(["x", "b"], ["y", "b"])] == ["replace", "equal"],
)
check(
    "缺内容的一侧槽位文本为空串",
    align_markdown_blocks(["a"], [])[0].right_text == ""
    and align_markdown_blocks(["a"], [])[0].right_idx is None,
)

# ── 2. 双栏对话框 ─────────────────────────────────────────────
dlg = VersionDiffDialog(
    name="note.md",
    left_label="v1 · 2026-09-15T09:00:00 · 10字",
    right_label="v2 · 2026-09-15T10:00:00 · 20字",
    left_text="keep1\nold only\nkeep2\nkeep3",
    right_text="keep1\nkeep2\nnew only\nkeep3",
    mode="markdown",
    dirty_hint=True,
)
check("对话框两栏进入源码态", dlg.left_edit.current_mode() == "markdown")
left_bg = {s.format.background().color().name() for s in dlg.left_edit.editor().extraSelections()}
right_bg = {
    s.format.background().color().name() for s in dlg.right_edit.editor().extraSelections()
}
check("删除行左栏红底", "#3a2026" in left_bg, str(left_bg))
check("新增行右栏绿底", "#173321" in right_bg, str(right_bg))
check("左栏含补位空行底色", "#0b0e13" in left_bg, str(left_bg))
check(
    "两栏物理行数一致（补位对齐）",
    dlg.left_edit.editor().document().blockCount()
    == dlg.right_edit.editor().document().blockCount(),
)
check("差异计数文本", dlg.lbl_diff_counter.text() == "差异 0/2", dlg.lbl_diff_counter.text())
dlg._goto_diff(1)
check("下一处差异后计数更新", dlg.lbl_diff_counter.text() == "差异 1/2")
check(
    "差异定位高亮已铺上",
    any(
        s.format.background().color().name() == "#223c63"
        for s in dlg.left_edit.editor().extraSelections()
    ),
)
check(
    "关闭自动换行",
    dlg.left_edit.editor().lineWrapMode()
    == QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap,
)
# 光标移动（编辑器自身逻辑会清空 ExtraSelections）后高亮应被补回
cur = dlg.left_edit.editor().textCursor()
cur.movePosition(cur.MoveOperation.NextBlock)
dlg.left_edit.editor().setTextCursor(cur)
check(
    "光标移动后删除行红底仍在",
    "#3a2026"
    in {
        s.format.background().color().name()
        for s in dlg.left_edit.editor().extraSelections()
    },
)
dlg.left_edit.verticalScrollBar().setValue(1)
dlg.right_edit.verticalScrollBar().setValue(1)
check("同步滚动不递归崩溃", True)
dlg.close()

# ── 2b. 窗口内切换版本 ─────────────────────────────────────
_store = {1: "aaa\nbbb", 2: "aaa\nccc", 3: "zzz"}
_versions = [(1, "v1 旧"), (2, "v2"), (3, "v3 新")]
dlg2 = VersionDiffDialog(
    name="s.md",
    left_label="v2",
    right_label="v3 新",
    left_text=_store[2],
    right_text=_store[3],
    mode="text",
    versions=_versions,
    version_loader=lambda vid: _store.get(vid),
    left_version_id=2,
    right_version_id=3,
)
check("可切换模式生成版本下拉", hasattr(dlg2, "combo_left_ver") and hasattr(dlg2, "combo_right_ver"))
check("左下拉默认选中 v2", dlg2.combo_left_ver.currentData() == 2)
check("右下拉默认选中 v3", dlg2.combo_right_ver.currentData() == 3)
check("初始右侧内容", dlg2.right_edit.toPlainText().rstrip("\n") == "zzz")
# 右栏切到 v1：与左栏 v2 对比 → replace 行
idx_v1 = dlg2.combo_right_ver.findData(1)
dlg2.combo_right_ver.setCurrentIndex(idx_v1)
check("切换后右侧内容更新", dlg2.right_edit.toPlainText().rstrip("\n") == "aaa\nbbb")
check("切换后重算差异", dlg2._diff_rows == [1], str(dlg2._diff_rows))
check("切换后统计刷新", dlg2.lbl_diff_counter.text() == "差异 0/1", dlg2.lbl_diff_counter.text())
check(
    "切换后右栏出现 replace 黄底",
    "#33301b"
    in {s.format.background().color().name() for s in dlg2.right_edit.editor().extraSelections()},
)
# 切到不存在的版本（loader 返回 None）→ 回退选择
dlg2.combo_right_ver.setCurrentIndex(dlg2.combo_right_ver.findData(3))
_versions.append((9, "v9 丢失"))
dlg2.combo_right_ver.addItem("v9 丢失", 9)
dlg2.combo_right_ver.setCurrentIndex(dlg2.combo_right_ver.count() - 1)
check("取不到版本时回退选择", dlg2.combo_right_ver.currentData() == 3)
dlg2.close()

# ── 2c. 双模式：源码 / Markdown 预览 ──────────────────────────
def _line_colors(view) -> list[str]:
    """预览层文档里每行的块底色（黑 = 无底色）。"""
    out: list[str] = []
    block = view.document().firstBlock()
    while block.isValid():
        out.append(block.blockFormat().background().color().name())
        block = block.next()
    return out


def _fragment_colors(view) -> set[str]:
    """预览层文档里出现过的字符底色集合（黑 = 无底色）。"""
    out: set[str] = set()
    block = view.document().firstBlock()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                out.add(fragment.charFormat().background().color().name())
            iterator += 1
        block = block.next()
    return out


dlg3 = VersionDiffDialog(
    name="doc.md",
    left_label="v1",
    right_label="v2",
    left_text=md_left,
    right_text=md_right,
    mode="markdown",
)
check(
    "默认源码模式",
    dlg3._display_mode == "source" and dlg3._mode_buttons["source"].isChecked(),
)
check("Markdown 才有模式切换控件", set(dlg3._mode_buttons) == {"source", "preview"})
check(
    "默认显示源码编辑器、隐藏预览层",
    not dlg3.left_edit.isHidden() and dlg3._left_preview.isHidden(),
)
check("源码模式行号栏可见", not dlg3.left_edit.editor()._line_number_area.isHidden())
source_text_snapshot = dlg3.left_edit.toPlainText()
source_stats_snapshot = dlg3._stats_label.text()

dlg3._set_display_mode("preview")
check(
    "切到预览：隐藏两侧编辑器、显示两侧预览层",
    dlg3.left_edit.isHidden()
    and dlg3.right_edit.isHidden()
    and not dlg3._left_preview.isHidden()
    and not dlg3._right_preview.isHidden(),
)
check(
    "预览不借用编辑器（编辑器始终在源码态）",
    dlg3.left_edit.current_mode() == "markdown",
)
check("预览导航改按块槽位", dlg3._diff_nav_items() == [1, 5], str(dlg3._diff_nav_items()))
check("预览差异计数", dlg3.lbl_diff_counter.text() == "差异 0/2", dlg3.lbl_diff_counter.text())
check("预览模式统计口径不变", dlg3._stats_label.text() == source_stats_snapshot)
check(
    "预览模式说明「按行标出差异」",
    "按行标出差异" in dlg3._note_label.text(),
    dlg3._note_label.text(),
)

left_colors = _line_colors(dlg3._left_preview)
# 行级差异现在是「渲染结果里的行内 span 底色」，所以看字符格式而不是块格式
left_span_colors = _fragment_colors(dlg3._left_preview)
right_span_colors = _fragment_colors(dlg3._right_preview)
check(
    "预览：改动块左侧按行标出修改行",
    PREVIEW_BLOCK_COLORS["replace"] in left_span_colors,
    str(left_span_colors),
)
check(
    "预览：改动块右侧按行标出修改行",
    PREVIEW_BLOCK_COLORS["replace"] in right_span_colors,
    str(right_span_colors),
)
check(
    "预览：新增块右栏按行标出绿底",
    PREVIEW_BLOCK_COLORS["insert"] in right_span_colors,
    str(right_span_colors),
)
check(
    "预览：左栏对应位置为占位空行（块级底色）",
    PREVIEW_BLOCK_COLORS["blank"] in left_colors,
    str(left_colors),
)
check("预览：未改动块不带任何差异底色", "#000000" in left_colors, str(left_colors))
check(
    "预览：未改动块仍渲染 Markdown（标题可见）",
    "标题" in dlg3._left_preview.toPlainText(),
    dlg3._left_preview.toPlainText()[:40],
)
check(
    "预览：改动块的代码块也渲染（围栏语言标签可见）",
    "py" in dlg3._left_preview.toPlainText(),
    dlg3._left_preview.toPlainText()[:120],
)

# 多行改动块：只有真正改动的行上底色，未变行保持原底（否则整块糊成一片看不出改在哪）
ctx_dlg = VersionDiffDialog(
    name="ctx.md",
    left_label="v1",
    right_label="v2",
    left_text="第一行\n第二行\n第三行",
    right_text="第一行\n第二行改了\n第三行",
    mode="markdown",
)
ctx_dlg._set_display_mode("preview")
ctx_span_colors = _fragment_colors(ctx_dlg._left_preview)
check(
    "多行改动块：未变行不上底色",
    PREVIEW_CONTEXT_COLOR not in ctx_span_colors,
    str(ctx_span_colors),
)
check(
    "多行改动块：改动行用修改底色",
    PREVIEW_BLOCK_COLORS["replace"] in ctx_span_colors,
    str(ctx_span_colors),
)
ctx_dlg.close()

# 列表 / 表格这类会被渲染重排的结构：只给变动的那一项上色，未变项保持原底
block_dlg = VersionDiffDialog(
    name="tbl.md",
    left_label="v1",
    right_label="v2",
    left_text="| A | B |\n|---|---|\n| 1 | 1 |\n\n- 甲\n- 乙",
    right_text="| A | B |\n|---|---|\n| 1 | 2 |\n\n- 甲\n- 丙",
    mode="markdown",
)
block_dlg._set_display_mode("preview")
table_block_colors = _line_colors(block_dlg._left_preview)
check(
    "表格 / 列表：变动块标色",
    PREVIEW_BLOCK_COLORS["replace"] in table_block_colors,
    str(table_block_colors),
)
check(
    "表格 / 列表：未变项保持原底（不是整块一片）",
    "#000000" in table_block_colors and len(set(table_block_colors)) > 1,
    str(table_block_colors),
)
check(
    "退回整块标色时仍然渲染（表格分隔行不再以源码出现）",
    "|---|" not in (block_dlg._left_preview.toPlainText() or ""),
    block_dlg._left_preview.toPlainText()[:80],
)
block_dlg.close()

check("预览层只读", dlg3._left_preview.isReadOnly())
preview_text_before = dlg3._left_preview.toPlainText()
_key = QtGui.QKeyEvent(
    QtCore.QEvent.Type.KeyPress,
    QtCore.Qt.Key.Key_X,
    QtCore.Qt.KeyboardModifier.NoModifier,
    "x",
)
QtWidgets.QApplication.sendEvent(dlg3._left_preview, _key)
check("预览态输入不改内容", dlg3._left_preview.toPlainText() == preview_text_before)

dlg3._goto_diff(1)
check("预览模式导航计数更新", dlg3.lbl_diff_counter.text() == "差异 1/2", dlg3.lbl_diff_counter.text())
check(
    "预览导航可定位到差异槽位",
    dlg3._left_preview.scroll_to_slot(1) and dlg3._right_preview.scroll_to_slot(5),
)
check("越界槽位定位返回 False", dlg3._left_preview.scroll_to_slot(99) is False)

dlg3._set_display_mode("source")
check(
    "切回源码：显示编辑器、隐藏预览层",
    not dlg3.left_edit.isHidden() and dlg3._left_preview.isHidden(),
)
check("切回源码：行号栏恢复", not dlg3.left_edit.editor()._line_number_area.isHidden())
check(
    "切回源码：文本与首次源码逐行一致",
    dlg3.left_edit.toPlainText() == source_text_snapshot,
    repr(dlg3.left_edit.toPlainText()[:80]),
)
check(
    "切回源码：整行高亮恢复",
    "#33301b"
    in {s.format.background().color().name() for s in dlg3.left_edit.editor().extraSelections()},
)
dlg3.close()

# 非 Markdown 内容不出现模式切换控件
dlg_log = VersionDiffDialog(
    name="a.log",
    left_label="v1",
    right_label="v2",
    left_text="a\nb",
    right_text="a\nc",
    mode="log",
)
check("非 Markdown 不构建模式切换控件", not hasattr(dlg_log, "_mode_buttons"))
dlg_log.close()

# 切版本时保持当前模式
_md_store = {1: md_left, 2: md_right, 3: md_left}
dlg4 = VersionDiffDialog(
    name="s.md",
    left_label="v1",
    right_label="v2",
    left_text=_md_store[1],
    right_text=_md_store[2],
    mode="markdown",
    versions=[(1, "v1"), (2, "v2"), (3, "v3")],
    version_loader=lambda vid: _md_store.get(vid),
    left_version_id=1,
    right_version_id=2,
)
dlg4._set_display_mode("preview")
dlg4.combo_left_ver.setCurrentIndex(dlg4.combo_left_ver.findData(3))
check(
    "窗口内切版本后保持预览模式",
    dlg4._display_mode == "preview" and not dlg4._left_preview.isHidden(),
)
dlg4.close()

# 块数超限：拒绝进入预览并给出可见原因（不静默降级）
_big_text = "\n\n".join(f"块{i}" for i in range(MAX_PREVIEW_BLOCKS + 1))
dlg_big = VersionDiffDialog(
    name="big.md",
    left_label="v1",
    right_label="v2",
    left_text=_big_text,
    right_text=_big_text,
    mode="markdown",
)
dlg_big._set_display_mode("preview")
check(
    "超限拒绝进入预览并说明原因",
    dlg_big._display_mode == "source" and "已保持源码模式" in dlg_big._note_label.text(),
    dlg_big._note_label.text(),
)
check("超限后切换控件回到源码", dlg_big._mode_buttons["source"].isChecked())
check("超限后仍显示源码编辑器", not dlg_big.left_edit.isHidden())
dlg_big.close()

# ── 3. 对比下拉框默认值（最新 vs 上一版）────────────────────────
panel = ui_mod.RightPanel()
check(
    "版本行新增 2 下拉 + 1 按钮",
    all(
        hasattr(panel, n)
        for n in ("label_diff", "combo_diff_left", "combo_diff_right", "label_diff_arrow", "btn_diff")
    ),
)


class _ComboHost:
    pass


host = _ComboHost()
host._qt_is_valid = lambda widget: widget is not None
for _name in ("combo_diff_left", "combo_diff_right", "btn_diff"):
    setattr(host, _name, getattr(panel, _name))
host._set_diff_enabled = types.MethodType(ui_mod.MainWindow._set_diff_enabled, host)
populate = types.MethodType(ui_mod.MainWindow._populate_diff_combos, host)

versions = [
    {"id": 30, "saved_at": "2026-09-15T10:00:00", "length": 30, "preview": "c"},
    {"id": 20, "saved_at": "2026-09-15T09:00:00", "length": 20, "preview": "b"},
    {"id": 10, "saved_at": "2026-09-15T08:00:00", "length": 10, "preview": "a"},
]
populate(versions, reset=True)
check(
    "默认 = 最新 vs 上一版",
    host.combo_diff_right.itemData(host.combo_diff_right.currentIndex()) == 30
    and host.combo_diff_left.itemData(host.combo_diff_left.currentIndex()) == 20,
)
host.combo_diff_left.setCurrentIndex(0)
populate(versions)
check(
    "刷新时保留用户选择",
    host.combo_diff_left.itemData(host.combo_diff_left.currentIndex()) == 10,
)
populate(versions, reset=True)
check(
    "切换内容后回到默认",
    host.combo_diff_left.itemData(host.combo_diff_left.currentIndex()) == 20,
)
populate([versions[0]])
check("只有一个版本时禁用对比", not host.btn_diff.isEnabled())
populate(None)
check("无版本时禁用对比且下拉为空项", not host.btn_diff.isEnabled())

# 同一版本 / 无版本时点「对比」：只提示，不弹窗
host.status = _StatusStub()
host.state = SimpleNamespace(dirty=False)
host._populate_version_combo = lambda **kwargs: None
host._diff_mode_from_filename = lambda name: "text"
host._on_diff_clicked = types.MethodType(ui_mod.MainWindow._on_diff_clicked, host)
populate(versions, reset=True)
host.combo_diff_right.setCurrentIndex(host.combo_diff_left.currentIndex())
host._on_diff_clicked()
check(
    "两侧选同一版本时不弹窗",
    host.status.messages == ["请选择两个不同的版本再对比"],
    str(host.status.messages),
)

_mode_host = SimpleNamespace()
_mode_host._mode_from_filename = types.MethodType(
    ui_mod.MainWindow._mode_from_filename, _mode_host
)
mode_fn = types.MethodType(ui_mod.MainWindow._diff_mode_from_filename, _mode_host)
check("对比栏 md 用源码态", mode_fn("a.md") == "markdown" and mode_fn("a.log") == "log")

# ── 4. 外部文件重载后写入版本历史（需求 B）────────────────────
recorded: list[tuple] = []
_orig_add_version = history_store.add_version


def _fake_add_version(kind, ref, title, content):
    recorded.append((kind, ref, title, content))
    return True


history_store.add_version = _fake_add_version
try:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "external.md"
        target.write_text("外部改后的新内容\n第二行\n", encoding="utf-8")
        reload_host = SimpleNamespace(
            _qt_is_valid=lambda widget: widget is not None,
            content_edit=_EditorStub(),
            status=_StatusStub(),
            state=SimpleNamespace(dirty=True, current_note_id=None),
            _ask_ai_mode=False,
            _current_external_file=str(target),
            _current_ipc_file=None,
            _current_server_log_path=None,
        )
        for _name, _fn in (
            ("_current_version_context", ui_mod.MainWindow._current_version_context),
            ("_record_version", ui_mod.MainWindow._record_version),
            ("_mode_from_filename", ui_mod.MainWindow._mode_from_filename),
            ("_reload_local_file_from_disk", ui_mod.MainWindow._reload_local_file_from_disk),
        ):
            setattr(reload_host, _name, types.MethodType(_fn, reload_host))
        reload_host._invalidate_log_content_cache = lambda p: None
        reload_host._record_loaded_local_file = lambda p=None: None
        reload_host._set_code_editor_status_file = lambda p: None
        reload_host._set_code_editor_status_text = lambda *a: None
        reload_host._update_title = lambda: None
        reload_host._sync_version_combo_on_open = lambda: None
        reload_host._editor_scroll_ratio = lambda editor: 0.0
        reload_host._restore_editor_scroll_ratio = lambda editor, ratio: None

        reload_host._reload_local_file_from_disk(target)
        expected_content = target.read_text(encoding="utf-8")
    check(
        "重载后写入版本（external + 磁盘全文）",
        recorded == [("external", str(target), "external.md", expected_content)],
        str(recorded),
    )
finally:
    history_store.add_version = _orig_add_version

# ── 5. 相对路径图片：降级为占位，不报错（既有限制）────────────
img_dlg = VersionDiffDialog(
    name="img.md",
    left_label="v1",
    right_label="v2",
    left_text="段落一\n\n![图](不存在/图片.png)\n\n段落二",
    right_text="段落一\n\n![图](不存在/图片.png)\n\n段落二改了",
    mode="markdown",
)
img_dlg._set_display_mode("preview")
check(
    "含相对路径图片仍能进入预览",
    img_dlg._display_mode == "preview" and not img_dlg._left_preview.isHidden(),
)
check(
    "图片不解析时其余内容照常渲染",
    "段落一" in img_dlg._left_preview.toPlainText(),
    img_dlg._left_preview.toPlainText()[:60],
)
img_dlg.close()

# ── 6. 大正文：同步渲染的耗时与差异标出 ──────────────────────
import time  # noqa: E402  （仅本段用到）

# 注意：块按「空行」切分，标题与正文之间夹空行会算成两个块。这里刻意不夹空行，
# 600 块 × ~690 字符 ≈ 414KB，用来验证「大正文进预览」的耗时与结果。
_filler = "填充文本" * 170
_big_blocks = [f"## 块{i}\n{_filler}" for i in range(600)]
_big_left = "\n\n".join(_big_blocks)
_big_right_blocks = list(_big_blocks)
_big_right_blocks[300] = f"## 块300\n改过的内容 {_filler}"
_big_right = "\n\n".join(_big_right_blocks)
check(
    "构造的大正文块数在上限内",
    len(markdown_block_texts(_big_left)) == 600
    and len(markdown_block_texts(_big_left)) < MAX_PREVIEW_BLOCKS,
    str(len(markdown_block_texts(_big_left))),
)

big_dlg = VersionDiffDialog(
    name="bigdoc.md",
    left_label="v1",
    right_label="v2",
    left_text=_big_left,
    right_text=_big_right,
    mode="markdown",
)
_t0 = time.time()
big_dlg._set_display_mode("preview")
_elapsed = time.time() - _t0
check(
    "大正文可进入预览",
    big_dlg._display_mode == "preview" and not big_dlg._right_preview.isHidden(),
)
check("大正文预览耗时 < 3s", _elapsed < 3.0, f"{_elapsed:.2f}s")
check(
    "大正文差异行仍被标出",
    PREVIEW_BLOCK_COLORS["replace"] in _line_colors(big_dlg._right_preview),
)
big_dlg.close()

print()
if FAILS:
    print(f"{len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("全部通过")
