# -*- coding: utf-8 -*-
"""
Explorer 窗口 UIAutomation 控件树分析工具 (comtypes 版)
"""
import ctypes
import time

import comtypes.client as cc

from pytracemp import lprint

# UIAutomation 常量
TreeScope_Children = 0x2
UIA_ClassNamePropertyId   = 30012
UIA_NamePropertyId        = 30005
UIA_ControlTypePropertyId = 30003

CTRL_TYPE_NAMES = {
    50000:"Button",50001:"Calendar",50002:"CheckBox",50003:"ComboBox",
    50004:"Edit",50005:"Hyperlink",50006:"Image",50007:"ListItem",
    50008:"List",50009:"Menu",50010:"MenuBar",50011:"MenuItem",
    50012:"ProgressBar",50013:"RadioButton",50014:"ScrollBar",
    50015:"Slider",50016:"Spinner",50017:"StatusBar",50018:"Tab",
    50019:"TabItem",50020:"Text",50021:"ToolBar",50022:"ToolTip",
    50023:"Tree",50024:"TreeItem",50025:"Custom",50026:"Group",
    50027:"Thumb",50028:"DataGrid",50029:"DataItem",50030:"Document",
    50031:"SplitButton",50032:"Window",50033:"Pane",50034:"Header",
    50035:"HeaderItem",50036:"Table",50037:"TitleBar",50038:"Separator",
    50039:"SemanticZoom",50040:"AppBar",
}


def main():
    # 加载 UIAutomation 类型库
    lprint("正在初始化 UIAutomation...")
    from comtypes import GUID as cGUID
    # CUIAutomation8 CLSID
    clsid = cGUID("{ff48dba4-60ef-4201-aa87-54103eef594e}")
    uia = cc.CreateObject(clsid, clsctx=0x1 | 0x2)

    # 加载 UIAutomationClient 类型库 (UIAutomationCore.dll)
    # 先 GetModule 加载类型库定义
    cc.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient
    iuia = uia.QueryInterface(UIAutomationClient.IUIAutomation)

    # 获取根元素
    root = iuia.GetRootElement()
    lprint(f"根元素: {root.CurrentName!r}  class={root.CurrentClassName!r}")

    # 查找 Explorer 窗口
    cond = iuia.CreatePropertyCondition(
        UIA_ClassNamePropertyId, "CabinetWClass"
    )
    explorers = root.FindAll(TreeScope_Children, cond)
    count = explorers.Length
    lprint(f"\n找到 {count} 个 Explorer 窗口")

    for i in range(count):
        exp = explorers.GetElement(i)
        lprint(f"\n{'='*70}")
        lprint(f"Explorer: name={exp.CurrentName!r}  class={exp.CurrentClassName!r}")
        lprint(f"  ctrlType={CTRL_TYPE_NAMES.get(exp.CurrentControlType, '?')}")
        lprint(f"{'='*70}")
        dump_uia_element(iuia, exp, depth=0, max_depth=15)

    lprint("\n\n===== 分析完成 =====")


def dump_uia_element(iuia, elem, depth=0, max_depth=15):
    """递归 dump UIA 元素"""
    if depth > max_depth:
        return

    indent = "  " * depth
    cls = elem.CurrentClassName or ""
    name = elem.CurrentName or ""
    ct = elem.CurrentControlType
    ctrl = CTRL_TYPE_NAMES.get(ct, f"?{ct}")

    try:
        aid = elem.CurrentAutomationId or ""
    except Exception:
        aid = ""

    try:
        lct = elem.CurrentLocalizedControlType or ""
    except Exception:
        lct = ""

    parts = [f"{indent}[{ctrl}]"]
    if cls:  parts.append(f"class={cls!r}")
    if name: parts.append(f"name={name!r}")
    if aid:  parts.append(f"autoId={aid!r}")
    if lct and lct != ctrl: parts.append(f"lct={lct!r}")
    lprint("  ".join(parts))

    # 查找子元素
    try:
        true_cond = iuia.CreateTrueCondition()
        children = elem.FindAll(TreeScope_Children, true_cond)
        n = children.Length
        for j in range(n):
            child = children.GetElement(j)
            dump_uia_element(iuia, child, depth + 1, max_depth)
    except Exception as e:
        if depth < 3:
            lprint(f"{'  ' * (depth+1)}(error: {e})")


if __name__ == "__main__":
    main()
