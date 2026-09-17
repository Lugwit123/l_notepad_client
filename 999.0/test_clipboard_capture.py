# -*- coding: utf-8 -*-
"""验证：剪贴板捕获引擎与单例历史存储（漏记修复）。

覆盖：
- 旧历史文件兼容加载且不静默去重
- GUI 线程阻塞期间的连续复制全部被记录（独立监听线程，不依赖 GUI 空闲）
- 重复复制同一内容 → 置顶 + 刷新时间，不新增行
- 应用自身写剪贴板 → 不回环录制
- 文件条目 / 图片条目（DIB）解析，图片与缩略图落盘
- 读取失败重试：被新内容取代则放弃；未取代则补记；重试耗尽留痕
- flush 落盘、stop 幂等与线程收敛

注意：测试用 Qt 跑在 offscreen 平台，其 ``QClipboard`` 并非系统剪贴板，
因此所有"外部复制"都通过子进程用原生 Win32 写系统剪贴板来产生
``WM_CLIPBOARDUPDATE``（同进程写剪贴板不会给自己发通知）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 隔离数据目录：必须在导入 l_notepad_client 之前设置（paths 首次解析后缓存）
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="lnp_clip_test_"))
_DATA_DIR = _TMP_ROOT / "data"
_CONFIG = _TMP_ROOT / "config.yaml"
_CONFIG.write_text(f"data_dir: {_DATA_DIR.as_posix()}\n", encoding="utf-8")
os.environ["WUWO_CONFIG_FILE"] = str(_CONFIG)

from PySide6 import QtCore, QtGui, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import clipboard_recorder as rec_mod
from l_notepad_client import clipboard_store as store_mod

FAILS: list[str] = []

# 子进程用原生 Win32 写系统剪贴板（13=CF_UNICODETEXT / 15=CF_HDROP / 8=CF_DIB）
_RAW_WRITER = r"""
import ctypes, struct, sys, time
mode = sys.argv[1]
value = sys.argv[2] if len(sys.argv) > 2 else ""
u = ctypes.windll.user32
k = ctypes.windll.kernel32
k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
k.GlobalAlloc.restype = ctypes.c_void_p
k.GlobalLock.argtypes = [ctypes.c_void_p]
k.GlobalLock.restype = ctypes.c_void_p
k.GlobalUnlock.argtypes = [ctypes.c_void_p]
u.OpenClipboard.argtypes = [ctypes.c_void_p]
u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

def put(fmt, data):
    handle = k.GlobalAlloc(0x0002, len(data))
    ptr = k.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    k.GlobalUnlock(handle)
    if not u.SetClipboardData(fmt, handle):
        raise OSError("SetClipboardData failed")

opened = False
for _ in range(20):
    if u.OpenClipboard(None):
        opened = True
        break
    time.sleep(0.05)
if not opened:
    raise OSError("OpenClipboard failed")
u.EmptyClipboard()
if mode == "text":
    put(13, (value + "\x00").encode("utf-16-le"))
elif mode == "file":
    put(15, struct.pack("<Iiiii", 20, 0, 0, 0, 1)
        + value.encode("utf-16-le") + b"\x00\x00\x00\x00")
elif mode == "image":
    w, h = 8, 6
    pixels = b"\x00\x00\xff\x00" * (w * h)   # BGRA 纯红
    put(8, struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(pixels), 0, 0, 0, 0) + pixels)
u.CloseClipboard()
print("raw set ok", mode)
"""


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def pump(seconds: float = 0.6) -> None:
    """让主线程处理事件（模拟界面回到消息循环）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def external_copy(mode: str, value: str = "") -> None:
    """模拟其它程序复制：子进程写系统剪贴板。"""
    subprocess.run([sys.executable, "-c", _RAW_WRITER, mode, value], check=False)


def texts(model) -> list[str]:
    return [str(it.get("text", "")) for it in model.items()]


def seed_legacy_history() -> list[dict]:
    """预置旧版本生成的历史文件（含重复条目与图片引用）。"""
    favorites = _DATA_DIR / "favorites"
    (favorites / "clipboard_images").mkdir(parents=True, exist_ok=True)
    image_path = favorites / "clipboard_images" / "deadbeefdeadbeef.png"
    legacy = [
        {"kind": "text", "text": "legacy-dup", "time": "2026-01-01 00:00:00"},
        {"kind": "text", "text": "legacy-dup", "time": "2026-01-01 00:00:01"},
        {"kind": "file", "text": "n", "files": ["C:/nonexistent/legacy.txt"],
         "count": 1, "time": "2026-01-01 00:00:02"},
        {"kind": "image", "text": "[图片]", "md5": "deadbeefdeadbeef",
         "image_path": str(image_path), "width": 4, "height": 4,
         "time": "2026-01-01 00:00:03"},
    ]
    (favorites / "clipboard_history.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    return legacy


def main() -> int:
    legacy = seed_legacy_history()
    store = store_mod.ClipboardHistoryStore.instance()
    recorder = rec_mod.instance()
    recorder.start(store.submit_item)
    pump(0.8)
    check("监听引擎已启动", recorder.active)

    # ── 1. 旧历史文件兼容加载 ──
    loaded_texts = texts(store.model)
    check("旧历史条目全部加载", len(store.model.items()) == len(legacy),
          f"{len(store.model.items())} != {len(legacy)}")
    check("旧历史不静默去重", loaded_texts.count("legacy-dup") == 2, str(loaded_texts))
    check("旧历史图片条目保留",
          any(it.get("kind") == "image" for it in store.model.items()))

    # ── 2. GUI 线程阻塞期间连续复制（期间不处理事件）──
    for t in ("clip-a", "clip-b", "clip-c"):
        external_copy("text", t)
        time.sleep(0.5)
    pump(1.0)
    captured = texts(store.model)
    check("阻塞期间三连复制全部记录",
          all(t in captured for t in ("clip-a", "clip-b", "clip-c")), str(captured[:6]))

    # ── 3. 重复复制 → 置顶 + 刷新时间，不新增行 ──
    row_b = store.model.row_of_key(("text", "clip-b"))
    check("clip-b 已在历史中", row_b is not None)
    old_time = store.model.items()[row_b].get("time") if row_b is not None else ""
    before_rows = len(store.model.items())
    time.sleep(0.6)
    external_copy("text", "clip-b")
    pump(1.0)
    items = store.model.items()
    check("重复复制不新增行", len(items) == before_rows, f"{len(items)} != {before_rows}")
    check("重复复制置顶", items[0].get("text") == "clip-b", str(items[0].get("text")))
    check("重复复制刷新时间", items[0].get("time") != old_time,
          f"{items[0].get('time')} == {old_time}")
    check("重复复制只保留一条", texts(store.model).count("clip-b") == 1)

    # ── 4. 自产写入不回环 ──
    rec_mod.write_to_clipboard(text="self-written")
    time.sleep(0.6)
    external_copy("text", "self-written")
    pump(1.0)
    check("应用自写不回环", "self-written" not in texts(store.model),
          str(texts(store.model)[:4]))
    time.sleep(0.6)
    external_copy("text", "other-written")
    pump(1.0)
    check("抑制窗口外的同类型复制仍记录", "other-written" in texts(store.model))

    # ── 5. 文件条目 ──
    sample = _TMP_ROOT / "sample.txt"
    sample.write_text("x", encoding="utf-8")
    time.sleep(0.6)
    external_copy("file", str(sample))
    pump(1.0)
    expect_file = os.path.normpath(str(sample))
    file_items = [it for it in store.model.items() if it.get("kind") == "file"]
    check("文件条目已记录",
          any(expect_file in (it.get("files") or []) for it in file_items),
          str([it.get("files") for it in file_items][:3]))

    # ── 6. 图片条目（CF_DIB → PNG）+ 落盘 ──
    time.sleep(0.6)
    external_copy("image")
    pump(1.2)
    fresh_images = [
        it for it in store.model.items()
        if it.get("kind") == "image" and it.get("width") == 8
    ]
    check("图片条目已记录", bool(fresh_images),
          str([(it.get("width"), it.get("height")) for it in
               store.model.items() if it.get("kind") == "image"][:3]))
    check("图片尺寸解析正确",
          all(it.get("height") == 6 and it.get("md5") for it in fresh_images))

    # ── 7. 读取失败重试链 ──
    listener = rec_mod._Win32ClipboardListener(recorder) if sys.platform == "win32" else None
    if listener is not None:
        before_sup = recorder.counters["superseded"]
        listener._retry_seq = -12345            # 与当前序列号必然不同
        listener._on_retry_timer()
        check("重试前被新内容取代则放弃",
              recorder.counters["superseded"] == before_sup + 1)

        time.sleep(0.6)
        external_copy("text", "retry-target")
        time.sleep(0.3)
        pump(0.6)
        before_ok = recorder.counters["retry_success"]
        listener._retry_seq = rec_mod._user32.GetClipboardSequenceNumber()
        listener._on_retry_timer()
        check("重试确认未取代则补记",
              recorder.counters["retry_success"] == before_ok + 1)
        pump(0.6)
        check("补记内容进入历史", "retry-target" in texts(store.model))

        before_fail = recorder.counters["read_failures"]
        listener._retry_left = 1
        listener._retry_seq = rec_mod._user32.GetClipboardSequenceNumber()
        listener._read_clipboard = lambda: ("no_data", None, None)
        listener._on_retry_timer()
        del listener._read_clipboard
        check("重试耗尽后留痕", recorder.counters["read_failures"] == before_fail + 1)

    # ── 8. flush 落盘 ──
    check("flush 在超时前完成", store.flush(6.0))
    try:
        on_disk = json.loads(store_mod.history_file().read_text(encoding="utf-8"))
    except Exception as e:
        on_disk = []
        print(f"  读取历史文件失败: {e}")
    check("磁盘条目数与内存一致", len(on_disk) == len(store.model.items()),
          f"{len(on_disk)} != {len(store.model.items())}")
    if fresh_images:
        png_path = str(fresh_images[0].get("image_path", ""))
        check("PNG 原图已落盘", os.path.exists(png_path), png_path)
        check("缩略图已生成",
              os.path.exists(png_path.replace(".png", "_t.png")), png_path)
    check("无残留 tmp 文件", not list((_DATA_DIR / "favorites").glob("*.tmp")))

    # ── 9. stop 幂等 + 线程收敛 ──
    time.sleep(1.0)                     # 让监听线程与写盘线程进入空闲
    cpu_before = time.process_time()
    time.sleep(2.0)
    cpu_idle = time.process_time() - cpu_before
    check("空闲期几乎不耗 CPU（无轮询/无定时器空转）", cpu_idle < 0.1,
          f"{cpu_idle:.3f}s")

    recorder.stop(3.0)
    recorder.stop(3.0)
    pump(0.3)
    alive = [t.name for t in threading.enumerate()]
    check("监听线程已收敛", "clipboard-listener" not in alive, str(alive))

    store.request_save(immediate=True)
    check("退出前落盘完成", store.flush(6.0))
    store.shutdown(3.0)
    started = time.monotonic()
    store.shutdown(3.0)          # 退出路径会重复调用，不得空等
    store.shutdown(3.0)
    check("重复 shutdown 不空等", time.monotonic() - started < 1.0,
          f"{time.monotonic() - started:.2f}s")

    print("")
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
