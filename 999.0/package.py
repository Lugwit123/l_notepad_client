# -*- coding: utf-8 -*-
# === wuwo doc_pkg BEGIN v7 (auto-generated, do not edit) ===
# 包：l_notepad_client 999.0  L Notepad PySide6 desktop client: local/offline notes + HTTP to l_notepad_server
# 依赖：python-3.12.10, pyside6, l_qframelesswindow, l_qt_wgt_lib, pytracemp, lugwit_baidu_netdisk
# 提供：PYTHONPATH {root}/src；env L_NOTEPAD_ROOT；PYTHONIOENCODING=utf-8
# 入口：l_notepad_client, l_notepad_ori
# 用法：wuwo l_notepad_client 进入该包环境；wuwor l_notepad_client -- l_notepad_client 直接调用别名
# 目录：包自有代码根：src/l_notepad_client/
#    自有子目录：doc、static
#    不要在源码根下盲搜全量文件，先按上面目录定位；第三方库的问题不在本包职责内。
# 查文档：全量搜索（笔记+全部知识库）：curl "http://127.0.0.1:8765/api/search?q=<关键词>&limit=10" → hits[].kb_name / rel
#     只搜本仓文档：curl "http://127.0.0.1:8765/api/kb/rez_pkg/search?q=<关键词>&limit=10"
#     读正文：curl "http://127.0.0.1:8765/api/kb/<kb_name>/workspace/file?path=<rel>"（参数名 path，不是 rel）
#     本机直连 8765 免 token（仅 GET，勿经 nginx）；q 要 URL 编码；score < 1.0 多为向量噪声
#     接口全表与踩坑见 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_notepad_搜索接口使用文档.md
# 跑代码：远程连上宿主程序，用宿主解释器跑代码（依赖与 Qt 事件循环齐备），别自己另起解释器
#     探活：curl http://127.0.0.1:8764/status（多实例逐个试 8764 / 8769；editor_available=true 才可用）
#     执行：curl -H "Content-Type: application/json" -d "{\"code\": \"print(1)\"}" http://127.0.0.1:8764/execute
#     长代码先落盘再 exec(open(r'<abs.py>', encoding='utf-8').read())，避免 JSON 转义地狱
#     其它工具：/execute_async 异步、/upload /upload_folder /download 传文件、/tools 列 agent 工具
#     UI 自动化（Qt 版 Playwright）：/ui/tree 控件树、/ui/locate 定位、/ui/action click|set_text|press_key、/ui/wait auto-wait、/ui/screenshot 截图
#     全表见 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez_pkg/l_script_editor.md
# 建包：新建/改包前先读 D:/TD_Depot/Software/Lugwit_syncPlug/lugwit_insapp/trayapp/rez-package-source/Rez-Docs/Rez包创建和启动指导文档.md
#    覆盖：目录布局 <包名>/<版本>/package.py、requires 写法、alias/env、变体哈希、修饰符与启动方式
# 规范：999.0 源码即环境（改源码即生效，无需 build）；依赖写进 requires 由 wuwo 自动补齐
#    修饰符 .dev_mod / .solo / .soloignore / .script_server 与建包规范见 Rez-Docs/Rez包创建和启动指导文档.md
# === wuwo doc_pkg END ===

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
    alias("l_notepad_client", "python -m l_notepad_client.local_main")
    # 原始模式：不使用自定义无边框标题栏，使用系统原生标题栏
    alias("l_notepad_ori", "python -m l_notepad_client.local_main_ori")
