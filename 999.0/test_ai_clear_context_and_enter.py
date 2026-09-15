# -*- coding: utf-8 -*-
"""问AI：清空上下文按钮 + 输入框回车发送 回归测试（offscreen）。"""
from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtGui, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import ui as ui_mod
from l_qt_wgt_lib.smart_widget import CodeEditorWidget

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


class _StatusStub:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def showMessage(self, text, timeout=0):
        self.messages.append(text)

    def told(self, needle: str) -> bool:
        return any(needle in m for m in self.messages)


class _TestWindow(ui_mod.MainWindow):
    """只初始化 QWidget，跳过 MainWindow.__init__ 的整套 UI 构建。"""

    def __init__(self) -> None:
        QtWidgets.QWidget.__init__(self)


def make_window(*, in_flight: bool = False, messages: list | None = None):
    win = _TestWindow()
    win.status = _StatusStub()
    win._ask_ai_mode = True
    win._current_ai_session_id = "s1"
    session = ui_mod.AiSession(
        session_id="s1",
        title="问AI 1",
        messages=list(messages or []),
        in_flight=in_flight,
    )
    win._ai_sessions = {"s1": session}
    win._save_settings = lambda: None
    win._update_token_labels = lambda: None
    # 一个真实的 AI 标签页：输入框 + 回答框（_get_ai_tab_* 从这里取）
    tab = QtWidgets.QWidget()
    content = CodeEditorWidget(tab)
    answer = CodeEditorWidget(tab)
    tab.setProperty("session_id", "s1")
    tab.setProperty("content_edit", content)
    tab.setProperty("ai_answer_edit", answer)
    tabs = QtWidgets.QTabWidget()
    tabs.addTab(tab, "问AI 1")
    win.ai_tabs = tabs
    # eventFilter 的其余分支会摸到这些
    win.content_edit = CodeEditorWidget(win)
    win.ai_answer_edit = answer
    win.log_view = CodeEditorWidget(win)
    win.help_view = None
    win._notes_tree_mode = False
    win.notes_tree = None
    win.notes_list = None
    return win, session, content, answer


def key_event(key, mods=QtCore.Qt.KeyboardModifier.NoModifier):
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods, "")


# ── 1. 清空上下文 ──────────────────────────────────────────
print("清空上下文")
win, session, content, answer = make_window(
    messages=[
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]
)
session.streaming_text = "半截流式"
answer.setPlainText("旧回答渲染文本")
# 草稿与输入框同步（真实流程里输入变化会把草稿写回 session）
session.draft_prompt = "待发问题"
content.setPlainText("待发问题")
win._clear_ai_context()
check("对话历史已清空", session.messages == [], str(session.messages))
check("流式/推理文本复位", session.streaming_text == "" and session.reasoning_text == "")
check("回答区显示已清空", answer.toPlainText().strip() == "", repr(answer.toPlainText()))
check("状态栏给出提示", win.status.told("已清空对话上下文"), str(win.status.messages))
check("输入框草稿保留", content.toPlainText() == "待发问题", repr(content.toPlainText()))

# ── 2. 边界：非 AI 模式 / 无会话 / 请求中 ────────────────────
print("清空上下文的边界")
win2, session2, _, _ = make_window(messages=[{"role": "user", "content": "hi"}])
win2._ask_ai_mode = False
win2._clear_ai_context()
check("非 AI 模式不动历史", len(session2.messages) == 1, str(session2.messages))
check("非 AI 模式给出提示", win2.status.told("当前不在问AI"), str(win2.status.messages))

win3, session3, _, _ = make_window(messages=[{"role": "user", "content": "hi"}])
win3._current_ai_session_id = "不存在"
win3._clear_ai_context()
check("会话不存在时不动历史", len(session3.messages) == 1)
check("会话不存在给出提示", win3.status.told("当前会话不存在"), str(win3.status.messages))

win4, session4, _, _ = make_window(
    messages=[{"role": "user", "content": "hi"}], in_flight=True
)
win4._clear_ai_context()
check("请求中拒绝清空", len(session4.messages) == 1, str(session4.messages))
check("请求中给出提示", win4.status.told("正在请求中"), str(win4.status.messages))

# ── 3. 输入框回车发送 ──────────────────────────────────────
print("输入框回车发送")
win5, _, content5, _ = make_window()
sent: list[int] = []
win5._ask_ai = lambda: sent.append(1)
inner = content5.editor()

check("AI 输入框被识别", win5._is_ai_tab_input(inner) is True)
check("AI 输入框 viewport 被识别", win5._is_ai_tab_input(inner.viewport()) is True)
check(
    "笔记编辑器不算 AI 输入框",
    win5._is_ai_tab_input(win5.content_edit.editor()) is False,
)

handled = win5.eventFilter(inner, key_event(QtCore.Qt.Key.Key_Return))
check("回车触发发送", handled is True and sent == [1], f"handled={handled} sent={sent}")
sent.clear()
handled = win5.eventFilter(inner.viewport(), key_event(QtCore.Qt.Key.Key_Enter))
check("小键盘回车也发送", handled is True and sent == [1], f"handled={handled} sent={sent}")
sent.clear()
handled = win5.eventFilter(
    inner, key_event(QtCore.Qt.Key.Key_Return, QtCore.Qt.KeyboardModifier.ShiftModifier)
)
check(
    "Shift+回车不发送（留给换行）",
    handled is False and sent == [],
    f"handled={handled} sent={sent}",
)
handled = win5.eventFilter(win5.content_edit.editor(), key_event(QtCore.Qt.Key.Key_Return))
check("笔记编辑器回车不发送", handled is False and sent == [])

win6, _, content6, _ = make_window()
sent6: list[int] = []
win6._ask_ai = lambda: sent6.append(1)
win6._ask_ai_mode = False
handled = win6.eventFilter(content6.editor(), key_event(QtCore.Qt.Key.Key_Return))
check("非 AI 模式回车不发送", handled is False and sent6 == [])

# ── 4. 发送中重复回车不再发 ────────────────────────────────
print("发送中的并发保护")
win7, session7, _, _ = make_window()
session7.in_flight = True
win7._ask_ai()  # 真实 _ask_ai：in_flight 分支在任何控件访问之前就返回
check("请求中再次发送被拒", win7.status.told("正在请求中，请稍候"), str(win7.status.messages))

# ── 5. 接线（按钮 / 连接 / 工具提示）─────────────────────────
print("接线检查")
src = pathlib.Path(ui_mod.__file__).read_text(encoding="utf-8")
check("右侧面板注册了清空按钮", '"btn_ai_clear": self.btn_ai_clear' in src)
check(
    "清空按钮已连接处理函数",
    "self.btn_ai_clear.clicked.connect(self._clear_ai_context)" in src,
)
check("清空按钮有提示文案", "清空当前问AI 会话的对话历史" in src)
check("AI 标签页输入框装了事件过滤器", "target.installEventFilter(self)" in src)
check("发送按钮提示回车用法", "Shift+回车换行" in src)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL PASS")
