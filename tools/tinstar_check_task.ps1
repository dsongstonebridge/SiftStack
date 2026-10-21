# Run by the Windows task "TinStar sale check Mondays 9 AM" (tools\register_tinstar_task.ps1).
# The task fires Monday 9 AM, at logon, on unlock, on wake and every 15 minutes, and never
# wakes the PC. The Python side decides whether this week's check is still due, so this
# is safe to run any number of times: it is silent unless the check runs.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = if ($env:SIFTSTACK_PYTHON) { $env:SIFTSTACK_PYTHON }
      else { "C:\Users\dsong\OneDrive\Desktop\SiftStack\.venv\Scripts\python.exe" }
Set-Location $repo
& $py "src\tinstar_sale_check.py" scheduled
exit $LASTEXITCODE
