# -*- coding: utf-8 -*-
"""验证：三个收藏面板共享单例剪贴板历史（去多写者覆盖）。

覆盖：
- 面板不再各自创建历史模型，全部复用 store.model
- 外部复制后各面板的过滤代理都能看到同一条历史
- 任一面板删除/清空 → 其它面板同步（不会被别的面板写回复活）
- 面板内的复制动作走统一入口（不再回环录成历史条目）
- 编辑保存经 store 生效并落盘
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="lnp_clip_panel_"))
_DATA_DIR = _TMP_ROOT / "data"
_CONFIG = _TMP_ROOT / "config.yaml"
_CONFIG.write_text(f"data_dir: {_DATA_DIR.as_posix()}\n", encoding="utf-8")
os.environ["WUWO_CONFIG_FILE"] = str(_CONFIG)

from PySide6 import QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import clipboard_recorder as rec_mod
from l_notepad_client import clipboard_store as store_mod
from l_notepad_client.folder_favorites_widget import FolderFavoritesWidget

FAILS: list[str] = []

_RAW_WRITER = r"""
import ctypes, sys, time
text = sys.argv[1]
u = ctypes.windll.user32
k = ctypes.windll.kernel32
k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k.GlobalAlloc.restype = ctypes.c_void_p
k.GlobalLock.argtypes = [ctypes.c_void_p]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalUnlock.argtypes = [ctypes.c_void_p]
u.OpenClipboard.argtypes = [ctypes.c_void_p]
u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
opened = False
for _ in range(20):
    if u.OpenClipboard(None):
        opened = True
        break
    time.sleep(0.05)
if not opened:
    raise OSError("OpenClipboard failed")
u.EmptyClipboard()
data = (text + "\x00").encode("utf-16-le")
handle = k.GlobalAlloc(0x0002, len(data))
ptr = k.GlobalLock(handle)
ctypes.memmove(ptr, data, len(data))
k.GlobalUnlock(handle)
if not u.SetClipboardData(13, handle):
    raise OSError("SetClipboardData failed")
u.CloseClipboard()
print("raw set ok")
"""


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def pump(seconds: float = 0.6) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def external_copy(text: str) -> None:
    subprocess.run([sys.executable, "-c", _RAW_WRITER, text], check=False)


def texts(model) -> list[str]:
    return [str(it.get("text", "")) for it in model.items()]


def main() -> int:
    store = store_mod.ClipboardHistoryStore.instance()
    recorder = rec_mod.instance()
    recorder.start(store.submit_item)

    folder_panel = FolderFavoritesWidget()
    command_panel = FolderFavoritesWidget()
    command_panel.set_favorites_kind("command")
    url_panel = FolderFavoritesWidget()
    url_panel.set_favorites_kind("url")
    panels = [folder_panel, command_panel, url_panel]
    for panel in panels:
        panel.finalize_ui()
    pump(0.8)

    check("三个面板共享同一历史模型",
          all(p.clipboard_model is store.model for p in panels))
    check("三个面板共享同一 store",
          all(getattr(p, "_store", None) is store for p in panels))
    check("面板不再各自持有防抖写盘 timer",
          not any(hasattr(p, "_clipboard_save_timer") for p in panels))
    check("面板列表绑定各自的过滤代理",
          all(p.clipboard_list.model() is p._clipboard_proxy for p in panels))

    # 外部复制 → 三个面板都看得到
    time.sleep(0.5)
    external_copy("panel-shared-1")
    pump(1.0)
    check("外部复制进入共享模型", "panel-shared-1" in texts(store.model))
    check("三个面板代理均可见同一条",
          all(p._clipboard_proxy.rowCount() == store.model.rowCount() for p in panels),
          str([p._clipboard_proxy.rowCount() for p in panels]))

    # 面板内复制动作不回环（统一入口 + 抑制窗口）
    time.sleep(0.5)
    folder_panel._copy_path_to_clipboard("panel-self-copy")
    external_copy("panel-self-copy")
    pump(1.0)
    check("面板内复制不回环", "panel-self-copy" not in texts(store.model))

    # 一个面板删除 → 其它面板同步
    time.sleep(0.5)
    external_copy("panel-shared-2")
    pump(1.0)
    target = [it for it in store.model.items() if it.get("text") == "panel-shared-1"][0]
    before = store.model.rowCount()
    folder_panel._delete_clipboard_item(target)
    pump(0.4)
    check("删除后模型行数减少", store.model.rowCount() == before - 1,
          f"{store.model.rowCount()} != {before - 1}")
    check("删除后各面板同步不可见",
          all(p._clipboard_proxy.rowCount() == store.model.rowCount() for p in panels)
          and str(target.get("text", "")) not in texts(url_panel.clipboard_model))
    check("删除触发落盘", store.flush(6.0))

    # 编辑保存经 store（模拟预览/编辑弹窗保存）
    edited = dict(store.model.items()[0])
    edited["text"] = "panel-edited"
    store.update_item_at(0, edited)
    check("编辑保存生效", store.model.items()[0].get("text") == "panel-edited")
    check("编辑后落盘", store.flush(6.0))
    on_disk = json.loads(store_mod.history_file().read_text(encoding="utf-8"))
    check("磁盘包含编辑结果",
          any(str(it.get("text", "")) == "panel-edited" for it in on_disk))

    # 一个面板清空 → 全部清空
    url_panel._clear_clipboard_history_now()
    pump(0.4)
    check("清空后共享模型为空", store.model.rowCount() == 0)
    check("清空后各面板同步为空",
          all(p._clipboard_proxy.rowCount() == 0 for p in panels))
    check("清空后落盘为空", store.flush(6.0)
          and json.loads(store_mod.history_file().read_text(encoding="utf-8")) == [])

    recorder.stop()
    store.shutdown(3.0)

    print("")
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
