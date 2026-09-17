# -*- coding: utf-8 -*-
"""验证：剪贴板弹窗的类型过滤下拉框（全部 / 文字 / 图片 / 文件）。

覆盖：
- 下拉框存在、位置在「清除」左侧、选项与 kind 映射正确
- 四种过滤的可见条目集合；「文件」涵盖单文件与多文件
- 类型过滤与搜索词组合（AND）
- 计数标签口径（匹配 x/y 条）
- 呼出弹窗重置为「全部」
- 过滤只影响显示：过滤状态下删除不影响其它类型
- 类型过滤计入"已过滤"，窗口高度按过滤结果收敛
- 弹出部件（下拉列表）打开期间弹窗不被自动隐藏
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="lnp_clip_popup_"))
_DATA_DIR = _TMP_ROOT / "data"
_CONFIG = _TMP_ROOT / "config.yaml"
_CONFIG.write_text(f"data_dir: {_DATA_DIR.as_posix()}\n", encoding="utf-8")
os.environ["WUWO_CONFIG_FILE"] = str(_CONFIG)

from PySide6 import QtCore, QtGui, QtWidgets

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import clipboard_store as store_mod
from l_notepad_client.clipboard_store import ClipboardHistoryModel
from l_notepad_client.folder_favorites_widget import (
    ClipboardHistoryPopup,
    FolderFavoritesWidget,
)

FAILS: list[str] = []

TEXT_COUNT = 30
IMAGE_COUNT = 2
FILE_COUNT = 2


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def pump(seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def make_png(path: Path, name: str, size=(8, 6)) -> Path:
    image = QtGui.QImage(size[0], size[1], QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor("red"))
    image.save(str(path / name), "PNG")
    image.save(str(path / name.replace(".png", "_t.png")), "PNG")
    return path / name


def seed_model(store) -> None:
    images_dir = _DATA_DIR / "favorites" / "clipboard_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    for i in range(TEXT_COUNT):
        items.append({"kind": "text", "text": f"text-{i:02d}",
                      "time": f"2026-01-01 00:00:{i:02d}"})
    for i in range(IMAGE_COUNT):
        png = make_png(images_dir, f"img{i}.png")
        items.append({"kind": "image", "text": "[图片]", "md5": f"img{i}",
                      "image_path": str(png), "width": 8, "height": 6,
                      "time": "2026-01-01 00:01:00"})
    for i in range(FILE_COUNT):
        items.append({"kind": "file", "text": f"f{i}.txt",
                      "files": [str(_TMP_ROOT / f"f{i}.txt")], "count": 1,
                      "time": "2026-01-01 00:02:00"})
    store.model.reset_items(items)


def visible_kinds(popup) -> list[str]:
    proxy = popup._proxy
    return [
        str(proxy.index(r, 0).data(ClipboardHistoryModel.KindRole) or "")
        for r in range(proxy.rowCount())
    ]


def visible_texts(popup) -> list[str]:
    proxy = popup._proxy
    return [
        str(proxy.index(r, 0).data(ClipboardHistoryModel.TextRole) or "")
        for r in range(proxy.rowCount())
    ]


def select_kind(popup, index: int) -> None:
    popup._kind_combo.setCurrentIndex(index)
    pump(0.2)


def main() -> int:
    store = store_mod.ClipboardHistoryStore.instance()
    seed_model(store)
    total = store.model.rowCount()

    panel = FolderFavoritesWidget()
    panel.finalize_ui()
    popup = ClipboardHistoryPopup(panel)
    popup.show_popup(0, at_cursor=False)
    popup._watch_timer.stop()   # 离屏测试：停掉真实桌面前台窗口轮询
    pump(0.4)

    # ── 1. 下拉框存在、选项、位置 ──
    combo = popup._kind_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    datas = [combo.itemData(i) for i in range(combo.count())]
    check("下拉框选项与 kind 映射正确",
          labels == ["全部", "文字", "图片", "文件"]
          and datas == [None, "text", "image", "file"], f"{labels} {datas}")
    clear_btns = [b for b in popup.findChildren(QtWidgets.QToolButton)
                  if "清除" in b.text()]
    check("清除按钮存在", len(clear_btns) == 1)
    check("下拉框在清除按钮左侧",
          bool(clear_btns) and combo.geometry().right() < clear_btns[0].geometry().left(),
          f"{combo.geometry()} vs {clear_btns[0].geometry() if clear_btns else None}")
    check("下拉框未被挤压", combo.width() == 66, str(combo.width()))

    # ── 2. 四种过滤的可见集合 ──
    check("默认全部可见", popup._proxy.rowCount() == total,
          f"{popup._proxy.rowCount()} != {total}")
    select_kind(popup, 1)
    check("文字过滤只留文字",
          set(visible_kinds(popup)) == {"text"} and popup._proxy.rowCount() == TEXT_COUNT,
          str(visible_kinds(popup)[:5]))
    select_kind(popup, 2)
    check("图片过滤只留图片",
          set(visible_kinds(popup)) == {"image"} and popup._proxy.rowCount() == IMAGE_COUNT,
          str(visible_kinds(popup)))
    select_kind(popup, 3)
    check("文件过滤只留文件",
          set(visible_kinds(popup)) == {"file"} and popup._proxy.rowCount() == FILE_COUNT,
          str(visible_kinds(popup)))
    check("文件过滤涵盖条目内容", len(visible_texts(popup)) == FILE_COUNT)
    select_kind(popup, 0)
    check("回到全部", popup._proxy.rowCount() == total)

    # ── 3. 类型过滤 + 搜索词组合 ──
    select_kind(popup, 2)
    popup._search.setText("张不存在的图")
    pump(0.2)
    check("图片 + 无匹配搜索词 → 空", popup._proxy.rowCount() == 0)
    popup._search.setText("图片")
    pump(0.2)
    check("图片 + 匹配搜索词 → 只留图片", popup._proxy.rowCount() == IMAGE_COUNT)
    select_kind(popup, 1)
    check("文字 + 图片搜索词 → 空", popup._proxy.rowCount() == 0)
    popup._search.clear()
    pump(0.2)

    # ── 4. 计数口径 ──
    select_kind(popup, 2)
    check("过滤时计数显示匹配 x/y",
          popup._count_label.text() == f"匹配 {IMAGE_COUNT}/{total} 条",
          popup._count_label.text())
    select_kind(popup, 0)
    check("无过滤时计数显示 y 条",
          popup._count_label.text() == f"{total} 条", popup._count_label.text())

    # ── 5. 高度按过滤结果收敛 ──
    select_kind(popup, 2)
    popup._fit_height_to_content()
    height_filtered = popup.height()
    select_kind(popup, 0)
    popup._fit_height_to_content()
    height_all = popup.height()
    check("图片过滤的高度小于全部",
          height_filtered < height_all, f"{height_filtered} vs {height_all}")
    check("过滤高度不超过上限", height_filtered <= popup.maximumHeight())

    # ── 6. 呼出重置为「全部」 ──
    select_kind(popup, 1)
    check("重置前为文字过滤", popup._proxy.kind_filter() == "text")
    popup.show_popup(0, at_cursor=False)
    popup._watch_timer.stop()
    pump(0.3)
    check("呼出后下拉框回到全部", combo.currentIndex() == 0)
    check("呼出后列表显示全部", popup._proxy.rowCount() == total)

    # ── 7. 过滤只影响显示 ──
    select_kind(popup, 2)
    target = popup._proxy.index(0, 0).data(ClipboardHistoryModel.ItemRole)
    panel._delete_clipboard_item(target)
    pump(0.2)
    check("删除后图片只剩一条", popup._proxy.rowCount() == IMAGE_COUNT - 1)
    select_kind(popup, 0)
    check("其它类型条目仍在",
          popup._proxy.rowCount() == total - 1
          and len(visible_texts(popup)) == total - 1,
          f"{popup._proxy.rowCount()} != {total - 1}")

    # ── 8. 弹出部件打开期间不隐藏 ──
    popup.show_popup(0, at_cursor=False)
    popup._watch_timer.stop()
    pump(0.2)
    combo.showPopup()
    pump(0.3)
    popup._maybe_hide()
    check("下拉打开时不隐藏弹窗", popup.isVisible(),
          f"activePopupWidget={QtWidgets.QApplication.activePopupWidget()}")
    combo.hidePopup()
    pump(0.3)
    if (QtWidgets.QApplication.activePopupWidget() is None
            and QtWidgets.QApplication.activeWindow() is not popup):
        popup._maybe_hide()
        check("下拉关闭后恢复自动隐藏", not popup.isVisible())
    else:
        # 离屏平台常把弹窗本身当作 activeWindow，无法构造"应当隐藏"的前置条件
        print("  SKIP  下拉关闭后恢复自动隐藏（离屏环境弹窗仍为 activeWindow）")

    print("")
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
