# -*- coding: utf-8 -*-
"""验证网址收藏页图标放大 3 倍、其它收藏页保持紧凑。"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets, QtGui, QtCore

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from l_notepad_client import folder_favorites_widget as ffw

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def make_panel(kind: str):
    panel = ffw.FolderFavoritesWidget(None)
    panel.set_favorites_kind(kind)
    panel.finalize_ui()
    return panel


print("网址收藏页")
url_panel = make_panel("url")
check("图标 20x20（favicon 本体）", url_panel.list_widget.iconSize() == QtCore.QSize(20, 20),
      str(url_panel.list_widget.iconSize()))
check("图标用贴合画布", url_panel._favorites_icon_tight() is True)
check("行高 = 图标 + 1px 间距", url_panel._favorites_item_height() == 21,
      str(url_panel._favorites_item_height()))
pm = ffw._icon_with_cloud(
    url_panel.style().standardIcon(QtWidgets.QStyle.SP_FileLinkIcon),
    True,
    url_panel._favorites_icon_size(),
    tight=True,
).pixmap(20, 20)
check("贴合画布 = 图标边长（无留白）", pm.width() == 20 and pm.height() == 20,
      f"{pm.width()}x{pm.height()}")
img = pm.toImage()
non_empty = any(
    img.pixelColor(x, y).alpha() > 0 for x in range(0, 20, 2) for y in range(0, 20, 2)
)
check("组合图标非空白", non_empty)
check("画布四角无多余留白（左上角有内容）",
      img.pixelColor(1, 1).alpha() > 0 or any(
          img.pixelColor(0, y).alpha() > 0 for y in range(20)
      ))

print("文件夹收藏页")
folder_panel = make_panel("folder")
check("文件夹图标保持 14x14", folder_panel.list_widget.iconSize() == QtCore.QSize(14, 14),
      str(folder_panel.list_widget.iconSize()))
check("文件夹行高保持 22", folder_panel._favorites_item_height() == 22)
check("文件夹不用贴合画布", folder_panel._favorites_icon_tight() is False)
pm2 = ffw._icon_with_cloud(
    folder_panel.style().standardIcon(QtWidgets.QStyle.SP_DirIcon), False
).pixmap(22, 22)
check("文件夹组合图标画布 22", pm2.width() == 22 and pm2.height() == 22,
      f"{pm2.width()}x{pm2.height()}")

print("命令收藏页")
cmd_panel = make_panel("command")
check("命令图标保持 14x14", cmd_panel.list_widget.iconSize() == QtCore.QSize(14, 14),
      str(cmd_panel.list_widget.iconSize()))

print("样式表按实际控件名生效（真实 .ui 中网址列表名为 url_favorites_list）")
url_panel.list_widget.setObjectName("url_favorites_list")
url_panel._apply_favorites_list_compact_style()
check("网址页样式跟随控件名", "QListWidget#url_favorites_list" in url_panel.list_widget.styleSheet(),
      url_panel.list_widget.styleSheet())
check("网址页 min-height 21", "min-height: 21px" in url_panel.list_widget.styleSheet(),
      url_panel.list_widget.styleSheet())
check("切换种类后图标仍为 20", url_panel.list_widget.iconSize() == QtCore.QSize(20, 20),
      str(url_panel.list_widget.iconSize()))
check("文件夹页样式含 folder_favorites_list",
      "QListWidget#folder_favorites_list" in folder_panel.list_widget.styleSheet())

print("实际行高（visualItemRect）")
icon = url_panel.style().standardIcon(QtWidgets.QStyle.SP_FileLinkIcon)
for i in range(3):
    it = QtWidgets.QListWidgetItem(
        ffw._icon_with_cloud(
            icon, True, url_panel._favorites_icon_size(), tight=True
        ),
        f"  站点{i}",
    )
    it.setSizeHint(QtCore.QSize(0, url_panel._favorites_item_height()))
    url_panel.list_widget.addItem(it)
url_panel.resize(600, 400)
url_panel.show()
app.processEvents()
heights = {url_panel.list_widget.visualItemRect(url_panel.list_widget.item(i)).height()
           for i in range(3)}
check("网址页实际行高 21px", heights == {21}, str(heights))
check("行高 = 图标 20 + 间距 1", heights == {20 + 1}, str(heights))

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
