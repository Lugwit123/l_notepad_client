# -*- coding: utf-8 -*-
"""验证：硬盘上被删除的文件条目在列表中显示 ✕，点「刷新」清除。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets, QtGui, QtCore

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import ui as ui_mod

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


class _TestWindow(ui_mod.MainWindow):
    def __init__(self):
        QtWidgets.QWidget.__init__(self)


def make_window(tmp_root: Path):
    win = _TestWindow()
    win.status = _StatusStub()
    win.state = SimpleNamespace(current_note_id=None, dirty=False)
    win._ask_ai_mode = False
    win._current_external_file = None
    win._current_ipc_file = None
    win._current_server_log_path = None
    win._external_files = []
    win._ipc_files = []
    win._favorite_order = []
    win._external_files_state_path = lambda: tmp_root / "external_files.json"
    win._notepad_list_dir = lambda: tmp_root
    win._notes_tree_mode = False
    win.notes_tree = None
    win.notes_list = QtWidgets.QListWidget()
    win._refresh_calls = 0
    win.refresh_notes = lambda: setattr(win, "_refresh_calls", win._refresh_calls + 1)
    return win


def list_labels(win) -> list[str]:
    return [win.notes_list.item(i).text() for i in range(win.notes_list.count())]


tmp = Path(tempfile.mkdtemp(prefix="lnp_missing_"))
# 不污染真实用户目录：外部文件状态写盘到临时目录
ui_mod.paths.notes_dir = lambda: tmp  # type: ignore[assignment]

f_alive = tmp / "存在.md"
f_gone = tmp / "被删了.md"
f_alive.write_text("# alive", encoding="utf-8")
f_gone.write_text("# gone", encoding="utf-8")

print("删除标记计算")
win = make_window(tmp)
check("存在的文件 → ↗", win._file_entry_marker(str(f_alive)) == ("↗", False))
f_gone.unlink()
check("已删除的文件 → ✕", win._file_entry_marker(str(f_gone)) == ("✕", True))
check("tooltip 提示已删除",
      "已删除" in win._file_entry_tooltip(str(f_gone), True)
      and "刷新" in win._file_entry_tooltip(str(f_gone), True))

print("列表渲染：已删除条目显示 ✕ 且标红")
win._external_files = [str(f_alive), str(f_gone)]
win._ipc_files = [str(f_gone)]
win._refresh_notes_list([], query="")
labels = list_labels(win)
check("外部文件渲染出两条", sum(1 for t in labels if "md" in t) == 3, str(labels))
check("存在文件用 ↗", any("↗ 存在.md" in t for t in labels), str(labels))
check("删除文件用 ✕", sum(1 for t in labels if "✕ 被删了.md" in t) == 2, str(labels))

missing_item = next(
    win.notes_list.item(i) for i in range(win.notes_list.count())
    if "✕ 被删了.md" in win.notes_list.item(i).text()
)
check("删除条目文字标红",
      missing_item.foreground().color().name().lower() == "#ff6b6b",
      missing_item.foreground().color().name())
check("删除条目 tooltip 含提示",
      "已删除" in missing_item.toolTip(), missing_item.toolTip())
alive_item = next(
    win.notes_list.item(i) for i in range(win.notes_list.count())
    if "↗ 存在.md" in win.notes_list.item(i).text()
)
check("存在条目保持默认色",
      alive_item.foreground().color().name().lower() != "#ff6b6b",
      alive_item.foreground().color().name())

print("树渲染：✕ 与 delegate 告警色")
win_tree = make_window(tmp)
win_tree._notes_tree_mode = True
win_tree.notes_tree = QtWidgets.QTreeWidget()
win_tree.notes_list = None
win_tree._external_files = [str(f_alive), str(f_gone)]
win_tree._refresh_notes_tree([], query="")
tree_labels = []
root = win_tree.notes_tree.invisibleRootItem()
for i in range(root.childCount()):
    node = root.child(i)
    for j in range(node.childCount()):
        tree_labels.append(node.child(j).text(0))
check("树上删除条目用 ✕", any("✕ 被删了.md" in t for t in tree_labels), str(tree_labels))
check("树上存在条目用 ↗", any("↗ 存在.md" in t for t in tree_labels), str(tree_labels))
delegate = ui_mod._NoteTreeItemDelegate()
check("delegate 提供告警色", delegate._COLOR_MISSING.name().lower() == "#ff6b6b")

print("底部「刷新」清理已删除条目")
win2 = make_window(tmp)
win2._external_files = [str(f_alive), str(f_gone)]
win2._ipc_files = [str(f_gone)]
win2._current_external_file = str(f_gone)  # 正在查看的正是被删文件
win2._on_refresh_button_clicked()
check("外部文件只留存在项", win2._external_files == [str(f_alive)],
      str(win2._external_files))
check("IPC 列表同样清理", win2._ipc_files == [], str(win2._ipc_files))
check("当前文件指针清空", win2._current_external_file is None)
check("清理后仍触发刷新", win2._refresh_calls == 1)
check("状态栏报告清理数量",
      any("已清理 2" in m for m in win2.status.messages), str(win2.status.messages))
check("状态落盘", win2._external_files_state_path().is_file())

print("无删除项时只刷新")
win3 = make_window(tmp)
win3._external_files = [str(f_alive)]
win3._on_refresh_button_clicked()
check("无删除项不误删", win3._external_files == [str(f_alive)])
check("提示普通刷新", any("列表已刷新" in m for m in win3.status.messages),
      str(win3.status.messages))

print("当前文件被删 → 触发列表刷新（✕ 及时出现）")
win4 = make_window(tmp)
win4._current_external_file = str(f_gone)
win4._loaded_local_file_path = str(f_gone)
win4._loaded_local_file_mtime = 1.0
win4._loaded_local_file_size = 1
win4._local_file_reload_prompting = False
win4.isVisible = lambda: True
win4.isActiveWindow = lambda: True
win4._check_local_file_changed(source="测试")
check("文件消失后触发刷新", win4._refresh_calls == 1)
check("状态栏提示文件不存在",
      any("已不存在" in m for m in win4.status.messages), str(win4.status.messages))

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
