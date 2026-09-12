@echo off
setlocal
set "P3=D:\TD_Depot\Software\Lugwit_syncPlug\lugwit_insapp\trayapp\rez-package-3rd"
set "SRC=D:\TD_Depot\Software\Lugwit_syncPlug\lugwit_insapp\trayapp\rez-package-source"
set "PYTHONPATH=%P3%\.pyside6_merged\6.11.0"
set "PYTHONPATH=%PYTHONPATH%;%P3%\mistune\3.3.4\platform-windows\arch-AMD64\python-3.12\python"
set "PYTHONPATH=%PYTHONPATH%;%P3%\pygments\2.21.0\platform-windows\arch-AMD64\python-3.12\python"
set "QTPY=%P3%\qtpy"
for /d %%D in ("%QTPY%\*") do set "QPVER=%%~nxD"
set "PYTHONPATH=%PYTHONPATH%;%QTPY%\%QPVER%\platform-windows\arch-AMD64\python-3.12\python"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\l_thread_safe\999.0\src"
set "PYTHONPATH=%PYTHONPATH%;%P3%\pywin32\312\platform-windows\arch-AMD64\python-3.12\python;%P3%\pywin32\312\platform-windows\arch-AMD64\python-3.12\python\win32;%P3%\pywin32\312\platform-windows\arch-AMD64\python-3.12\python\win32\lib;%P3%\pywin32\312\platform-windows\arch-AMD64\python-3.12\python\Pythonwin"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\pytracemp\999.0\src"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\Lugwit_Module\999.0\src"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\l_qt_wgt_lib\999.0\src"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\l_qframelesswindow\999.0\src"
set "PYTHONPATH=%PYTHONPATH%;%SRC%\l_notepad_client\999.0\src"
set "QT_QPA_PLATFORM=offscreen"
"D:\TD_Depot\Software\Lugwit_syncPlug\lugwit_insapp\trayapp\wuwo\py_312\python.exe" %*
endlocal
