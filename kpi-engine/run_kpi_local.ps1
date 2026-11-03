# Runs one day's KPI posts on this PC: the same steps, in the same order, as the GitHub
# workflow kpi-posts.yml. Called by trigger_kpi_post.ps1 when GitHub will not start the run
# (2026-11-02: GitHub answered "Actions has been disabled for this repository").
# Safe to run again: steps already done for that day are kept in reports\kpi_local_done_<day>.txt
# and skipped, so a retry after a failure never posts anything twice.
# Exit 0 = every post went out. Exit 1 = a post failed (see reports\kpi_local_<day>.log).

param(
    [Parameter(Mandatory = $true)][string]$Date,       # YYYY-MM-DD
    [Parameter(Mandatory = $true)][string]$What,       # daily | both
    [string]$Texts = "",                               # base64 text stats for the Friday bot
    [switch]$CatchUp                                   # a missed day: refill the sheet from that day
)

$ErrorActionPreference = "Continue"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$botDir  = Join-Path (Split-Path -Parent $here) "kpi-bot"
$log     = Join-Path $here "reports\kpi_local_$Date.log"
$doneLog = Join-Path $here "reports\kpi_local_done_$Date.txt"
$env:PYTHONIOENCODING = "utf-8"

$done = @()
if (Test-Path $doneLog) { $done = @(Get-Content $doneLog) }

function Step($name, $dir, $cmd) {
    if ($done -contains $name) { return $true }
    "==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $name" | Out-File -Append -Encoding utf8 $log
    Push-Location $dir
    # cmd handles the redirect so Python's output reaches the log as plain UTF-8
    cmd /c "$cmd >> `"$log`" 2>&1"
    $rc = $LASTEXITCODE
    Pop-Location
    if ($rc -ne 0) { "==== $name FAILED, exit $rc" | Out-File -Append -Encoding utf8 $log; return $false }
    $name | Out-File -Append -Encoding ascii $doneLog
    return $true
}

$ledger = "reports/ledger.json"

# The sheet fill never blocks the posts (continue-on-error in the workflow)
$since = if ($CatchUp) { " --since $Date" } else { "" }
[void](Step "fill-sheet" $here "py -3 fill_sheet.py$since")

if (-not (Step "daily-post" $here "py -3 pull_kpis.py --from $Date --to $Date --slack auto --ledger $ledger")) { exit 1 }

if ($What -eq "both") {
    if (-not (Step "weekly-summary" $here "py -3 pull_kpis.py --week-of $Date --slack auto --ledger $ledger --note `"Weekly summary`"")) { exit 1 }

    # The sheet bot reports the week of KPI_AS_OF and takes its text stats from TEXT_KPIS
    $env:KPI_AS_OF = $Date
    $env:TEXT_KPIS = $Texts
    $ok = Step "weekly-sheet-post" $botDir "py -3 kpi_slack_bot.py"
    Remove-Item Env:KPI_AS_OF, Env:TEXT_KPIS -ErrorAction SilentlyContinue
    if (-not $ok) { exit 1 }
}
exit 0
