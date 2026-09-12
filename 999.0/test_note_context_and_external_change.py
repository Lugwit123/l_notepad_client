# -*- coding: utf-8 -*-
"""验证：笔记列表右键「打开文件所在文件夹」+ 本地文件外部修改检测。"""
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


# ── 构造只带必要属性的 MainWindow 替身（不走 __init__，避免拉起完整 UI）──
class _StatusStub:
    def __init__(self):
        self.messages: list[str] = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)


class _EditorStub:
    """只实现检测流程用到的编辑器接口。"""

    def __init__(self):
        self._text = ""

    def setPlainText(self, text):
        self._text = text

    def toPlainText(self):
        return self._text

    def load_text_file_cached(self, path, mode=None):
        self._text = Path(path).read_text(encoding="utf-8")
        return True


class _TestWindow(ui_mod.MainWindow):
    """只初始化 QWidget，跳过 MainWindow.__init__ 的整套 UI 构建。"""

    def __init__(self):
        QtWidgets.QWidget.__init__(self)


def make_window(tmp_root: Path, note_title: str):
    win = _TestWindow()
    win._ask_ai_mode = False
    win._current_server_log_path = None
    win._current_external_file = None
    win._current_ipc_file = None
    win.state = SimpleNamespace(current_note_id=1, dirty=False)
    win.title_edit = QtWidgets.QLineEdit(note_title)
    win.status = _StatusStub()
    win._loaded_local_file_path = None
    win._loaded_local_file_mtime = None
    win._loaded_local_file_size = None
    win._local_file_reload_prompting = False
    win._right_widget_refs = {}
    win.content_edit = _EditorStub()
    win._favorite_order = []
    win.notes_tree = None
    win.notes_list = QtWidgets.QListWidget()
    win._notepad_list_dir = lambda: tmp_root
    win._notes_tree_mode = False
    win._update_title = lambda: None
    win._sync_version_combo_on_open = lambda: None
    win._invalidate_log_content_cache = lambda *a, **k: None
    win.refresh_notes = lambda: None  # 刷新走后台线程，测试里不拉起
    # 与真实 __init__ 一致：文件监视 + 轮询兜底
    win._local_file_watcher = QtCore.QFileSystemWatcher(win)
    win._local_file_watcher.fileChanged.connect(win._on_local_file_watch_event)
    win._local_file_watcher.directoryChanged.connect(win._on_local_file_watch_event)
    win._local_file_poll_timer = QtCore.QTimer(win)
    win._local_file_poll_timer.setInterval(ui_mod._LOCAL_FILE_POLL_MS)
    win._local_file_poll_timer.timeout.connect(win._poll_local_file_changed)
    return win


tmp = Path(tempfile.mkdtemp(prefix="lnp_ext_"))
note = tmp / "笔记A.md"
note.write_text("原始内容", encoding="utf-8")

print("路径解析")
win = make_window(tmp, "笔记A.md")
check("笔记条目 → 磁盘路径",
      win._item_local_file_path(7, "笔记A.md") == note,
      str(win._item_local_file_path(7, "笔记A.md")))
check("外部文件条目 → 磁盘路径",
      win._item_local_file_path(ui_mod.EXTERNAL_FILE_PREFIX + r"D:\x\y.txt")
      == Path(r"D:\x\y.txt"))
check("IPC 文件条目 → 磁盘路径",
      win._item_local_file_path(ui_mod.IPC_FILE_PREFIX + r"D:\x\z.log")
      == Path(r"D:\x\z.log"))
check("文件夹/未知条目 → None", win._item_local_file_path("__folder__:a") is None)
check("笔记无标题 → None", win._item_local_file_path(7, "") is None)

print("资源管理器定位（monkeypatch Popen，不真的开窗口）")
launched: list[list[str]] = []
orig_popen = ui_mod.subprocess.Popen
ui_mod.subprocess.Popen = lambda args, *a, **k: launched.append(list(args))
try:
    win._open_path_in_explorer(note)
    check("存在的文件 → /select 定位",
          launched and launched[-1][0] == "explorer" and launched[-1][1] == "/select,"
          and launched[-1][2] == str(note), str(launched))
    win._open_path_in_explorer(tmp)
    check("文件夹 → 直接打开目录",
          launched[-1][0] == "explorer" and launched[-1][1] == str(tmp), str(launched))
    missing = tmp / "已删除.md"
    win._open_path_in_explorer(missing)
    check("文件已删除 → 退到父目录",
          launched[-1][0] == "explorer" and launched[-1][1] == str(tmp), str(launched))
finally:
    ui_mod.subprocess.Popen = orig_popen

print("外部修改检测（笔记文件，此前只对 .log 生效）")
win = make_window(tmp, "笔记A.md")
win._record_loaded_local_file(note)
check("记录快照", win._loaded_local_file_path == str(note)
      and win._loaded_local_file_mtime is not None)

# 外部改文件（mtime 必须比快照新）
import time as _time
_time.sleep(0.01)
note.write_text("外部改过的内容", encoding="utf-8")
os.utime(note, (_time.time() + 5, _time.time() + 5))

answered: list[str] = []
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append(str(a[2])), QtWidgets.QMessageBox.StandardButton.Yes)[1]
)
try:
    win._check_local_file_updated_on_foreground()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("弹窗提示已触发", len(answered) == 1, str(answered))
check("提示含文件名", answered and "笔记A.md" in answered[0], str(answered))
check("重载为磁盘新内容", win.content_edit.toPlainText() == "外部改过的内容",
      repr(win.content_edit.toPlainText()))
check("重载后记为干净", win.state.dirty is False)

print("无变化时不打扰")
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.No)[1]
)
try:
    win._check_local_file_updated_on_foreground()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("mtime 未变 → 不提示", answered == [], str(answered))

print("编辑器有未保存改动时也提示（旧实现直接静默跳过）")
note.write_text("又被外部改了", encoding="utf-8")
os.utime(note, (_time.time() + 60, _time.time() + 60))
win.state.dirty = True
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.No)[1]
)
try:
    win._check_local_file_updated_on_foreground()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("dirty 时不再静默跳过", len(answered) == 1, str(answered))
win.state.dirty = False

print("服务器日志/问AI 不参与本地文件检测")
win2 = make_window(tmp, "笔记A.md")
win2._current_server_log_path = "app/a.log"
win2.state.current_note_id = 1
check("服务器日志 → 无本地文件", win2._current_local_file_path() is None)
win2._current_server_log_path = None
win2._ask_ai_mode = True
check("问AI → 无本地文件", win2._current_local_file_path() is None)

print("外部文件路径也纳入检测")
win3 = make_window(tmp, "x")
ext = tmp / "外部.txt"
ext.write_text("v1", encoding="utf-8")
win3._current_external_file = str(ext)
check("外部文件 → 有本地文件", win3._current_local_file_path() == ext)

print("文件监视 / 轮询挂钩")
win4 = make_window(tmp, "笔记A.md")
win4._record_loaded_local_file(note)
check("监视文件本身", str(note) in win4._local_file_watcher.files(),
      str(win4._local_file_watcher.files()))
check("同时监视父目录（兼容原子替换写盘）",
      str(note.parent) in win4._local_file_watcher.directories(),
      str(win4._local_file_watcher.directories()))
check("轮询已启动", win4._local_file_poll_timer.isActive())
# 切到「无本地文件」的场景（问AI / 服务器日志）→ 停止轮询并清空监视
win4._ask_ai_mode = True
win4._clear_loaded_local_file()
check("无本地文件时停止轮询", not win4._local_file_poll_timer.isActive())
check("无本地文件时清空监视", not win4._local_file_watcher.files())
check("问AI 无本地文件", win4._current_local_file_path() is None)
win4._ask_ai_mode = False
win4._record_loaded_local_file(note)


def _force_active(w):
    w.isVisible = lambda: True
    w.isActiveWindow = lambda: True


def _force_inactive(w):
    w.isVisible = lambda: True
    w.isActiveWindow = lambda: False


print("窗口不在前台时后台轮询不弹模态框")
_force_inactive(win4)
win4._loaded_local_file_mtime = 1.0
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.No)[1]
)
try:
    win4._poll_local_file_changed()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("后台轮询静默", answered == [], str(answered))

print("轮询发现外部修改（源文件监视之外的兜底路径）")
_force_active(win4)
_time.sleep(0.01)
note.write_text("轮询发现的新内容", encoding="utf-8")
os.utime(note, (_time.time() + 120, _time.time() + 120))
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.Yes)[1]
)
try:
    win4._poll_local_file_changed()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("轮询提示重载", len(answered) == 1, str(answered))
check("轮询重载内容", win4.content_edit.toPlainText() == "轮询发现的新内容",
      repr(win4.content_edit.toPlainText()))

print("文件监视事件同样触发检测")
_time.sleep(0.01)
note.write_text("监视事件的新内容", encoding="utf-8")
os.utime(note, (_time.time() + 300, _time.time() + 300))
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.Yes)[1]
)
try:
    win4._on_local_file_watch_event(str(note))
finally:
    QtWidgets.QMessageBox.question = orig_q
check("监视事件提示重载", len(answered) == 1, str(answered))
check("监视事件重载内容", win4.content_edit.toPlainText() == "监视事件的新内容",
      repr(win4.content_edit.toPlainText()))

print("编辑器有未保存改动 → 仍提示（明确告知会丢弃）")
_time.sleep(0.01)
note.write_text("冲突内容", encoding="utf-8")
os.utime(note, (_time.time() + 600, _time.time() + 600))
win4.state.dirty = True
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append(str(a[2])), QtWidgets.QMessageBox.StandardButton.No)[1]
)
try:
    win4._check_local_file_updated_on_foreground()
finally:
    QtWidgets.QMessageBox.question = orig_q
check("dirty 也提示", len(answered) == 1, str(answered))
check("提示说明会丢弃未保存修改",
      answered and "丢弃" in answered[0] and "未保存" in answered[0], str(answered))
check("选否则保留编辑内容", win4.content_edit.toPlainText() == "监视事件的新内容")
check("选否后刷新快照不再重复提示", win4._loaded_local_file_mtime is not None)
win4.state.dirty = False

print("外部删除文件")
_time.sleep(0.01)
note.unlink()
win4._loaded_local_file_mtime = _time.time() - 10
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.Yes)[1]
)
try:
    win4._on_local_file_watch_event(str(note))
finally:
    QtWidgets.QMessageBox.question = orig_q
check("文件删除不弹重载框", answered == [], str(answered))
check("文件删除给出状态提示",
      any("不存在" in m for m in win4.status.messages), str(win4.status.messages))

print("顶层外壳窗口激活 → 触发检查（ui.MainWindow 是外壳内容子控件，收不到 WindowActivate）")
shell = QtWidgets.QWidget()
win5 = make_window(tmp, "笔记A.md")
win5.setParent(shell)
check("未跟踪文件时不挂钩", not getattr(win5, "_top_window_activation_filter", False))
win5._record_loaded_local_file(note)
check("跟踪文件后挂钩顶层窗口",
      getattr(win5, "_top_window_activation_filter", False) is True)
calls: list[int] = []
win5._check_local_file_updated_on_foreground = lambda: calls.append(1)
QtWidgets.QApplication.sendEvent(shell, QtCore.QEvent(QtCore.QEvent.Type.WindowActivate))
app.processEvents()
check("WindowActivate 触发检查", calls == [1], str(calls))
QtWidgets.QApplication.sendEvent(shell, QtCore.QEvent(QtCore.QEvent.Type.ActivationChange))
app.processEvents()
check("ActivationChange 触发检查", len(calls) == 2, str(calls))

print("端到端：真实 markdown 预览组件 + 外部修改 → 重载后查看组件更新")
from l_qt_wgt_lib.smart_widget import CodeEditorWidget

md_note = tmp / "预览笔记.md"
md_note.write_text("# 标题一\n\n内容一\n", encoding="utf-8")
win6 = make_window(tmp, "预览笔记.md")
real_editor = CodeEditorWidget(win6)
real_editor.resize(600, 400)
real_editor.show()
app.processEvents()
real_editor.load_text_file_cached(md_note, mode="markdown_preview")
app.processEvents()
win6.content_edit = real_editor
check("已进入 markdown 预览态", real_editor.is_markdown_preview_mode())
win6._record_loaded_local_file(md_note)

_time.sleep(0.01)
md_note.write_text("# 标题二\n\n外部改过的内容\n", encoding="utf-8")
os.utime(md_note, (_time.time() + 900, _time.time() + 900))
_force_active(win6)
answered.clear()
orig_q = QtWidgets.QMessageBox.question
QtWidgets.QMessageBox.question = staticmethod(
    lambda *a, **k: (answered.append("prompt"), QtWidgets.QMessageBox.StandardButton.Yes)[1]
)
try:
    win6._on_local_file_watch_event(str(md_note))
finally:
    QtWidgets.QMessageBox.question = orig_q
check("端到端：提示出现", len(answered) == 1, str(answered))
check("端到端：查看组件显示磁盘新内容",
      "外部改过的内容" in real_editor.toPlainText(), repr(real_editor.toPlainText()))
preview = getattr(real_editor.editor(), "_preview_view", None)
check("端到端：预览层已重渲染",
      preview is not None and preview.current_markdown() == "# 标题二\n\n外部改过的内容\n",
      repr(preview.current_markdown()) if preview else "no preview")
check("端到端：重载后为干净态", win6.state.dirty is False)

print("右键菜单：打开所在文件夹 / 复制文件路径 / 复制所在文件夹")
win_menu = make_window(tmp, "笔记A.md")
menu_actions: list[str] = []
_orig_exec = ui_mod.QtWidgets.QMenu.exec


def _fake_exec(self, *a, **k):
    menu_actions.extend(act.text() for act in self.actions())


ui_mod.QtWidgets.QMenu.exec = _fake_exec
try:
    # 列表模式：note 条目（UserRole=int + 标题）与外部文件条目
    lst = win_menu.notes_list
    item_note = QtWidgets.QListWidgetItem("笔记A.md")
    item_note.setData(QtCore.Qt.ItemDataRole.UserRole, 7)
    item_note.setData(QtCore.Qt.ItemDataRole.UserRole + 1, "笔记A.md")
    lst.addItem(item_note)
    menu_actions.clear()
    win_menu._on_notes_list_context_menu(lst.visualItemRect(item_note).center())
    check("列表菜单含三项",
          menu_actions == ["📂 打开文件所在文件夹", "📋 复制文件路径", "📁 复制所在文件夹"],
          str(menu_actions))

    item_ext = QtWidgets.QListWidgetItem("外部")
    item_ext.setData(
        QtCore.Qt.ItemDataRole.UserRole, f"{ui_mod.EXTERNAL_FILE_PREFIX}{note}"
    )
    lst.addItem(item_ext)
    menu_actions.clear()
    win_menu._on_notes_list_context_menu(lst.visualItemRect(item_ext).center())
    check("外部文件条目同样有三项",
          "📋 复制文件路径" in menu_actions and "📁 复制所在文件夹" in menu_actions,
          str(menu_actions))
finally:
    ui_mod.QtWidgets.QMenu.exec = _orig_exec

print("复制动作把路径写进剪贴板")
app.clipboard().setText("")
win_menu._copy_text_to_clipboard(str(note), label="文件路径")
check("复制完整文件路径", app.clipboard().text() == str(note), app.clipboard().text())
win_menu._copy_text_to_clipboard(str(note.parent), label="所在文件夹")
check("复制所在文件夹路径", app.clipboard().text() == str(note.parent), app.clipboard().text())
check("状态栏给出反馈", any("已复制文件路径" in m for m in win_menu.status.messages),
      str(win_menu.status.messages))
app.clipboard().setText("")
win_menu._copy_text_to_clipboard("")
check("空文本不覆盖剪贴板", app.clipboard().text() == "", repr(app.clipboard().text()))

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
