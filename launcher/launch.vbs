Set sh = CreateObject("WScript.Shell")
d = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\Ghost Studio"
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & d & "\ghost.ps1""", 0, False
