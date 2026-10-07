' Run a PowerShell script with no window at all.
' Scheduled tasks that call powershell.exe directly flash a console for a
' moment even with -WindowStyle Hidden (the window exists before PowerShell
' can hide it). wscript.exe has no console, and Run(..., 0) starts PowerShell
' hidden from the first frame. Waits for the script and returns its exit code
' so Task Scheduler's Last Result stays meaningful.
'
' Usage (task action):
'   wscript.exe "<repo>\tools\run_hidden.vbs" "<path to script.ps1>"
If WScript.Arguments.Count < 1 Then WScript.Quit 2
Set sh = CreateObject("WScript.Shell")
rc = sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & WScript.Arguments(0) & """", 0, True)
WScript.Quit rc
