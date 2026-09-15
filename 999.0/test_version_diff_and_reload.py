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

from PySide6 import QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import history_store, ui as ui_mod
from l_notepad_client.version_diff_dialog import (
    VersionDiffDialog,
    build_padded_lines,
    build_side_by_side,
    diff_row_indices,
    diff_stats,
    intra_line_spans,
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
check("差异行号=非 equal 行", diff_row_indices(rows) == [1, 3])

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

print()
if FAILS:
    print(f"{len(FAILS)} 项失败: {FAILS}")
    sys.exit(1)
print("全部通过")
