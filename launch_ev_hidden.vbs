' EV監視をウィンドウ無しで起動 (誤クローズで監視が死ぬ事故を防止)。ログ: ev_monitor.log
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\owner\project\.venv\jra-web"
sh.Run "cmd /c C:\Users\owner\project\.venv\jra-web\start_ev_auto.bat", 0, False
