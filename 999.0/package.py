# -*- coding: utf-8 -*-

name = "l_notepad_client"
version = "999.0"
description = "L Notepad PySide6 desktop client: local/offline notes + HTTP to l_notepad_server"
authors = ["Lugwit Team"]

# NOTE:
# - 客户端只依赖桌面栈（pyside6 + 无边框库），不含 fastapi/uvicorn。
# - 笔记双数据源：本地/离线走 LocalNotepadApi（直读本地文件）；服务器文件走
#   NotepadApi（HTTP → l_notepad_server，经 nginx /note）。
# - 认证经 lugwit_auth（1027）HTTP 接入；不直连用户库。
# - l_qframelesswindow 提供标题栏 + ServerConfigStore（server_config 复用同一份持久化）。
requires = [
    "python-3.12.10",
    "pyside6",
    "l_qframelesswindow",
    "l_qt_wgt_lib",
    "pytracemp",
    "lugwit_baidu_netdisk",
]

build_command = False
cachable = True
relocatable = True


def commands():
    env.PYTHONPATH.prepend("{root}/src")
    env.L_NOTEPAD_ROOT = "{root}"
    env.PYTHONIOENCODING = "utf-8"

    # Pure PC mode: local file-based notes, no backend process.
    # 使用 cmd /c 包装以设置 UTF-8 代码页
    alias("l_notepad", "python -m l_notepad_client.local_main")
    # 原始模式：不使用自定义无边框标题栏，使用系统原生标题栏
    alias("l_notepad_ori", "python -m l_notepad_client.local_main_ori")
    # Keep original behavior: launch UI with embedded backend service.
    # 需要本机装有 l_notepad_server 才能拉起内嵌后端；缺省走本地/HTTP 双数据源。
    alias("l_notepad_with_api", "python -m l_notepad_client.main")
