# -*- coding: utf-8 -*-
"""验证：笔记列表接收资源管理器拖入的文件 → 归类「外部文件」分组。"""
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
    win.state = SimpleNamespace(current_note_id=7, dirty=False)
    win._ask_ai_mode = False
    win._current_external_file = None
    win._current_ipc_file = None
    win._current_server_log_path = None
    win._external_files = []
    win._ipc_files = []
    win._external_files_state_path = lambda: tmp_root / "external_files.json"
    win._notepad_list_dir = lambda: tmp_root
    win._notes_tree_mode = False
    win.notes_tree = None
    win.notes_list = QtWidgets.QListWidget()
    win.refresh_notes = lambda: None          # 异步刷新的替身
    win._select_external_file = lambda p: True
    win._qt_is_valid = lambda w: w is not None
    return win


def mime_for(paths: list[str]) -> QtCore.QMimeData:
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(p) for p in paths])
    return mime


tmp = Path(tempfile.mkdtemp(prefix="lnp_drop_"))
f1 = tmp / "外部A.md"
f2 = tmp / "外部B.txt"
f1.write_text("# A", encoding="utf-8")
f2.write_text("B", encoding="utf-8")

print("拖入数据解析")
check("uri-list → 本地路径",
      ui_mod._local_files_from_mime(mime_for([str(f1), str(f2)])) == [str(f1), str(f2)])
plain = QtCore.QMimeData()
plain.setText("只是文本")
check("纯文本 → 空", ui_mod._local_files_from_mime(plain) == [])
check("空数据 → 空", ui_mod._local_files_from_mime(None) == [])
notlocal = QtCore.QMimeData()
notlocal.setUrls([QtCore.QUrl("https://example.com/a.md")])
check("网络 URL → 空", ui_mod._local_files_from_mime(notlocal) == [])

print("拖入 → 归类外部文件组并打开")
win = make_window(tmp)
win._add_dropped_external_files([str(f1), str(f2)])
check("两个文件都进外部文件组",
      win._external_files == [str(f1), str(f2)], str(win._external_files))
check("打开第一个拖入文件", win._current_external_file == str(f1),
      str(win._current_external_file))
check("清除 IPC 当前文件", win._current_ipc_file is None)
check("已持久化", win._external_files_state_path().is_file())
check("状态栏提示", any("外部文件" in m for m in win.status.messages),
      str(win.status.messages))


def _drop_event(paths) -> object:
    return QtGui.QDropEvent(
        QtCore.QPointF(5, 5), QtCore.Qt.DropAction.CopyAction, mime_for(paths),
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
    )


def drag_enter(win, mime) -> bool:
    ev = QtGui.QDragEnterEvent(
        QtCore.QPoint(5, 5), QtCore.Qt.DropAction.CopyAction, mime,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
    )
    return win._handle_external_file_drop(ev)


def drop(win, mime) -> bool:
    ev = QtGui.QDropEvent(
        QtCore.QPointF(5, 5), QtCore.Qt.DropAction.CopyAction, mime,
        QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
    )
    return win._handle_external_file_drop(ev)


def drag_leave(win) -> bool:
    return win._handle_external_file_drop(QtGui.QDragLeaveEvent())


print("笔记列表 viewport 上的拖放事件")
win2 = make_window(tmp)
check("URL 拖入被接受", drag_enter(win2, mime_for([str(f1)])) is True)
check("拖入时给出落点提示",
      getattr(win2, "_external_drop_hint_on", False) is True
      and any("松开" in m for m in win2.status.messages),
      str(win2.status.messages))
check("纯文本拖入不拦截", drag_enter(win2, plain) is False)
check("URL 落下被消费", drop(win2, mime_for([str(f1), str(f2)])) is True)
check("落下后归类外部文件",
      win2._external_files == [str(f1), str(f2)], str(win2._external_files))
check("落下后提示复位", win2._external_drop_hint_on is False)
check("纯文本落下不消费", drop(win2, plain) is False)
check("拖离不消费", drag_leave(win2) is False)

print("经 eventFilter 的接线（笔记列表 viewport）")
win2b = make_window(tmp)
vp = win2b.notes_list.viewport()
# 注意：QDragEnterEvent/QDropEvent 不接管 QMimeData 所有权，必须自己留引用，
# 否则临时对象被 GC，event.mimeData() 变成空数据（仅测试侧问题）
mime_keep = mime_for([str(f1)])
enter_ev = QtGui.QDragEnterEvent(
    QtCore.QPoint(5, 5), QtCore.Qt.DropAction.CopyAction, mime_keep,
    QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
)
check("eventFilter 吃掉 URL 拖入", win2b.eventFilter(vp, enter_ev) is True)
drop_ev = QtGui.QDropEvent(
    QtCore.QPointF(5, 5), QtCore.Qt.DropAction.CopyAction, mime_keep,
    QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier,
)
check("eventFilter 处理落下并归类",
      win2b.eventFilter(vp, drop_ev) is True
      and win2b._external_files == [str(f1)], str(win2b._external_files))

print("重复拖入：不重复添加，仅置顶并提示")
win3 = make_window(tmp)
win3._add_dropped_external_files([str(f1), str(f2)])
win3.status.messages.clear()
win3._add_dropped_external_files([str(f2)])
check("不重复添加", win3._external_files == [str(f2), str(f1)], str(win3._external_files))
check("提示已在列表中", any("已在" in m for m in win3.status.messages),
      str(win3.status.messages))
check("置顶并打开该文件", win3._current_external_file == str(f2))

print("无效拖入")
win4 = make_window(tmp)
win4._add_dropped_external_files([str(tmp)])  # 目录
check("目录不加进列表", win4._external_files == [], str(win4._external_files))
check("目录给出提示", any("没有可用文件" in m for m in win4.status.messages),
      str(win4.status.messages))
win4.status.messages.clear()
win4._add_dropped_external_files([str(tmp / "不存在.md")])
check("不存在的路径不加进列表", win4._external_files == [])
check("不存在给出提示", any("没有可用文件" in m for m in win4.status.messages))

print("拖入顺序保持（先拖的在前）")
win5 = make_window(tmp)
win5._add_dropped_external_files([str(f1)])
win5._add_dropped_external_files([str(f2)])
check("后拖的排在前面",
      win5._external_files == [str(f2), str(f1)], str(win5._external_files))
win5._add_dropped_external_files([str(f1), str(f2)])
check("同批拖入按拖入顺序",
      win5._external_files == [str(f1), str(f2)], str(win5._external_files))

print("树模式也走同一入口")
win6 = make_window(tmp)
win6._notes_tree_mode = True
win6.notes_tree = QtWidgets.QTreeWidget()
win6.notes_list = None
check("树 viewport 被识别", win6._notes_drop_viewport() is win6.notes_tree.viewport())
check("树上拖入被接受", drag_enter(win6, mime_for([str(f1)])) is True)
check("树上落下归类", drop(win6, mime_for([str(f1)])) is True
      and win6._external_files == [str(f1)], str(win6._external_files))

print("拖入的多文件只在外部文件组渲染（不混入 IPC 组）")
check("未写入 IPC 列表", win6._ipc_files == [])
check("外部文件状态已落盘", win6._external_files_state_path().is_file())

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
