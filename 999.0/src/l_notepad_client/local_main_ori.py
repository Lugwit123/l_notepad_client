# -*- coding: utf-8 -*-
"""原始模式入口：不使用自定义无边框标题栏，直接以系统原生标题栏显示内容窗口。

用于排查自定义标题栏（L_FramelessMainWindow 外壳）相关的显示/交互问题。
"""

from l_notepad_client.local_main import main


if __name__ == "__main__":
    raise SystemExit(main(use_frameless=False))
