' Launch the integrated suite (EV monitor + WIN5 + Perf, port 5005) hidden on weekends. Log: suite_monitor.log
' If the suite is already running, jra_suite.py exits via port_guard (no duplicate).
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\owner\project\.venv\jra-web"
sh.Run "cmd /c C:\Users\owner\project\.venv\jra-web\start_suite_auto.bat", 0, False
