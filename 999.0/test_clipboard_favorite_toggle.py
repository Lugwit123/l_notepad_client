# -*- coding: utf-8 -*-
"""回归：剪贴板条目「取消收藏」不得把进程打崩（收藏组 → 非收藏组的下移）。

背景：`clipboard_store._move_to_group_top` 把落点行号直接当 Qt 的
`destinationChild` 传。同 parent 往下移时 Qt 只接受 `落点 + 1`（传落点会落在
非法区间 `[row, row + 1]`，`beginMoveRows` 返回 False），而代码不看返回值照样
`endMoveRows()` → 访问违例、整进程崩（`l_notepad/crash_20260927.log`：
`_move_to_group_top` line 324 in `endMoveRows`）。

覆盖：
- 收藏（上移）/ 取消收藏（下移）在同一批数据上反复切换不崩
- 两种切换后：收藏项恒在前、条目集合不变（不丢不重）
- 每次切换后过滤代理的行序与源模型一致（信号成对，未错位）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="lnp_clip_fav_"))
_DATA_DIR = _TMP_ROOT / "data"
_CONFIG = _TMP_ROOT / "config.yaml"
_CONFIG.write_text(f"data_dir: {_DATA_DIR.as_posix()}\n", encoding="utf-8")
os.environ["WUWO_CONFIG_FILE"] = str(_CONFIG)

from PySide6 import QtCore, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client.clipboard_store import ClipboardHistoryModel, ClipboardHistoryStore

FAILS: list[str] = []

ITEM_COUNT = 6


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


store = ClipboardHistoryStore.instance()
model = store.model

# 与真实界面一致：模型外面套一层过滤代理 + 列表视图（行序错位会立刻在视图上放大）
proxy = QtCore.QSortFilterProxyModel()
proxy.setSourceModel(model)
proxy.setFilterRole(QtCore.Qt.DisplayRole)
view = QtWidgets.QListView()
view.setModel(proxy)

for i in range(ITEM_COUNT):
    store.submit_item({"kind": "text", "text": f"item-{i}"})
app.processEvents()

check("初始条目数", model.rowCount() == ITEM_COUNT, str(model.rowCount()))
check("初始代理行数", proxy.rowCount() == ITEM_COUNT, str(proxy.rowCount()))


def snapshot():
    """源模型 / 代理两侧的当前行序（用 TextRole 取裸文本，DisplayRole 带时间前缀）。"""
    return ([it["text"] for it in model.items()],
            [proxy.index(r, 0).data(ClipboardHistoryModel.TextRole)
             for r in range(proxy.rowCount())])


def assert_consistent(step):
    src, prx = snapshot()
    check(f"{step}：代理与源模型行序一致", src == prx, f"{src} != {prx}")
    check(f"{step}：条目总数不变", model.rowCount() == ITEM_COUNT, str(model.rowCount()))
    check(f"{step}：无重复条目", len(set(src)) == len(src), str(src))
    check(f"{step}：无丢失条目",
          set(src) == {f"item-{i}" for i in range(ITEM_COUNT)}, str(src))
    fav_flags = [bool(it.get("favorite")) for it in model.items()]
    check(f"{step}：收藏项恒置顶", fav_flags == sorted(fav_flags, reverse=True), str(fav_flags))


# 1) 收藏（上移）：从底部往上逐个打星
fav_order = [5, 3, 2, 0, 4, 1]
for i in fav_order:
    row = next(r for r, it in enumerate(model.items()) if it["text"] == f"item-{i}")
    assert store.toggle_favorite(model.items()[row]) is True
    assert_consistent(f"收藏 item-{i}")

check("全部收藏后收藏数", model.favorite_count() == ITEM_COUNT,
      str(model.favorite_count()))

# 2) 取消收藏（下移）= 崩溃路径：含「倒数第二个收藏项」等边界
for i in reversed(fav_order):
    row = next(r for r, it in enumerate(model.items()) if it["text"] == f"item-{i}")
    assert store.toggle_favorite(model.items()[row]) is False
    assert_consistent(f"取消收藏 item-{i}")

check("全部取消后收藏数归零", model.favorite_count() == 0, str(model.favorite_count()))

# 3) 中间态反复横跳：k 个收藏里逐个取消、再逐个收回，覆盖落到 [row, row+1] 的各种行号
for k in range(1, ITEM_COUNT + 1):
    for it in list(model.items())[:k]:
        row = next(r for r, x in enumerate(model.items()) if x["text"] == it["text"])
        store.set_favorite(model.items()[row], True)
    assert_consistent(f"{k} 个收藏")
    for _ in range(k):
        row = model.favorite_count() - 1  # 从收藏组末尾往前逐个取消
        store.set_favorite(model.items()[row], False)
    assert_consistent(f"{k} 个收藏全取消")

store.shutdown()

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
