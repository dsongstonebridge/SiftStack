# Presses "Run workflow" on the GitHub "KPI posts" workflow for each weekday whose 6:30 PM
# has passed and has not been posted yet. Run by the Windows task "KPI posts - 6:30 PM".
# Jeff, 2026-10-02: GitHub's own timer ran hours late; this fires on time, and if the PC was
# asleep or off it catches up as soon as it is back, posting the day that was missed (not today).
# Fridays post "both" (daily + weekly sheet). State: reports\kpi_post_last.txt (last day posted).

$ErrorActionPreference = "Stop"
$gh    = "C:\Program Files\GitHub CLI\gh.exe"
$repo  = "dsongstonebridge/SiftStack"
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $here "reports\kpi_post_last.txt"
$log   = Join-Path $here "reports\kpi_post_trigger.log"
New-Item -ItemType Directory -Force (Split-Path $state) | Out-Null

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding utf8 $log }

# Text KPIs (Jeff, 2026-10-16): the SMS agent's database is only on this PC, so they are
# worked out here and sent in with the run (sms_kpis.py). A failure never blocks the post;
# the Texts section then says the stats are not available. Returns base64 JSON or "".
function Get-TextStats($date, $what) {
    $ErrorActionPreference = "Continue"
    $b64 = Join-Path $here "reports\texts_$date.b64"
    try {
        Remove-Item $b64 -ErrorAction SilentlyContinue
        $py = Join-Path (Split-Path -Parent $here) ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { $py = "python" }
        $pyArgs = @((Join-Path $here "sms_kpis.py"), "--date", $date, "--b64-out", $b64)
        if ($what -eq "both") { $pyArgs += "--week" }
        & $py @pyArgs *> (Join-Path $here "reports\texts_last.log")
        if (Test-Path $b64) { return (Get-Content $b64 -Raw).Trim() }
        Log "text stats for $date not worked out (see reports\texts_last.log)"
    } catch { Log "text stats for $date failed: $_" }
    return ""
}

$now  = Get-Date
# Jeff, 2026-10-05: Oct 2 never posted. The PC slept through 6:30; a self-wake at 8:56 Sat
# started the missed run and sleep killed it. Jeff then used the PC 10:40-12:15 Sat, but
# unlocking is not a logon, so nothing retried until Monday. The task now also fires every
# 15 minutes and on wake/unlock; this script is safe to run any number of times.
$last = if (Test-Path $state) { [datetime]::ParseExact((Get-Content $state -Raw).Trim(), "yyyy-MM-dd", $null) }
        else { $now.Date.AddDays(-1) }

# every weekday after the last posted day whose 6:30 PM is already in the past (max 7 back)
$due = @()
$d = $last.AddDays(1)
if ($d -lt $now.Date.AddDays(-7)) { $d = $now.Date.AddDays(-7) }
while ($d -le $now.Date) {
    if ($d.DayOfWeek -ne "Saturday" -and $d.DayOfWeek -ne "Sunday" -and $now -ge $d.AddHours(18).AddMinutes(30)) {
        $due += $d
    }
    $d = $d.AddDays(1)
}
if (-not $due) { exit 0 }   # runs every 15 min; stay quiet unless something is due
Log "due: $(($due | ForEach-Object { $_.ToString('yyyy-MM-dd') }) -join ', ')"

foreach ($day in $due) {
    $what = if ($day.DayOfWeek -eq "Friday") { "both" } else { "daily" }
    $date = $day.ToString("yyyy-MM-dd")
    $ok = $false
    $texts = ""
    # right after waking, the network may not be up yet: retry for up to ~15 minutes
    for ($try = 1; $try -le 15 -and -not $ok; $try++) {
        if (-not $texts) { $texts = Get-TextStats $date $what }
        # on time: leave date blank (same as pressing the button; sheet refills from yesterday)
        $ghArgs = @("workflow", "run", "kpi-posts.yml", "--repo", $repo, "-f", "what=$what")
        if ($day.Date -ne (Get-Date).Date) { $ghArgs += @("-f", "date=$date") }
        if ($texts) { $ghArgs += @("-f", "texts=$texts") }
        & $gh @ghArgs 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $ok = $true } else { Start-Sleep -Seconds 60 }
    }
    if (-not $ok) { Log "FAILED to start $what for $date after 15 tries"; exit 1 }
    $date | Out-File -Encoding ascii -NoNewline $state
    Log "started $what for $date$(if ($day.Date -ne $now.Date) { ' (catch-up)' })"
    if ($due.Count -gt 1) { Start-Sleep -Seconds 90 }   # one run at a time
}
exit 0
