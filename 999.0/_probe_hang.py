# -*- coding: utf-8 -*-
"""诊断包装器：跑指定测试，超时后转储所有线程栈并退出（不会无限挂住终端）。

用法：run_client_tests.bat _probe_hang.py <被诊断的测试文件> [超时秒]
"""
import faulthandler
import os
import runpy
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

target = sys.argv[1] if len(sys.argv) > 1 else "test_note_context_and_external_change.py"
limit = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
sys.argv = [target]

print(f"[probe] 运行 {target}，超时 {limit:.0f}s 后转储栈", flush=True)
faulthandler.dump_traceback_later(limit, exit=True)
runpy.run_path(target, run_name="__main__")
faulthandler.cancel_dump_traceback_later()
print("[probe] 正常结束", flush=True)
