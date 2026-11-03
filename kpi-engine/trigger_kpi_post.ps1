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

$env:PYTHONIOENCODING = "utf-8"   # sms_kpis prints emoji; without this it exits 1 on cp1252

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding utf8 $log }

# One Slack message per day when a post could not go out, so a miss is never silent.
function Alert-Once($m) {
    $flag = Join-Path $here "reports\kpi_alert_$(Get-Date -Format 'yyyy-MM-dd').txt"
    if (Test-Path $flag) { return }
    try {
        $line = Get-Content (Join-Path (Split-Path -Parent $here) "kpi-bot\.env") | Where-Object { $_ -match '^KPI_SLACK_WEBHOOK_URL=' } | Select-Object -First 1
        $hook = ($line -replace '^KPI_SLACK_WEBHOOK_URL=', '').Trim().Trim('"').Trim("'")
        Invoke-RestMethod -Method Post -Uri $hook -ContentType "application/json" -Body (@{ text = ":warning: $m" } | ConvertTo-Json) | Out-Null
        $m | Out-File -Encoding ascii $flag
    } catch { Log "Slack alert failed: $_" }
}

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

# Jeff, 2026-11-03: the first time the PC is open the next morning, compare the 6:30 numbers
# with the day's real totals; a fresh "Updated KPI's" post goes out only if they changed.
# kpi_recheck.py does nothing when no day is waiting. A failure here never blocks a post.
function Run-Recheck {
    $ErrorActionPreference = "Continue"
    $recheckLog = Join-Path $here "reports\kpi_recheck.log"
    Push-Location $here
    try { cmd /c "py -3 kpi_recheck.py >> `"$recheckLog`" 2>&1" } catch { Log "next-morning check failed: $_" }
    Pop-Location
    $ErrorActionPreference = "Stop"
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
if (-not $due) { Run-Recheck; exit 0 }   # runs every 15 min; stay quiet unless something is due
Log "due: $(($due | ForEach-Object { $_.ToString('yyyy-MM-dd') }) -join ', ')"

foreach ($day in $due) {
    $what = if ($day.DayOfWeek -eq "Friday") { "both" } else { "daily" }
    $date = $day.ToString("yyyy-MM-dd")
    $ok = $false
    $texts = ""
    $ghError = ""
    $refused = $false
    # right after waking, the network may not be up yet: retry for up to ~15 minutes.
    # A refusal from GitHub (2026-11-02: "Actions has been disabled") will not change, so don't wait.
    for ($try = 1; $try -le 15 -and -not $ok -and -not $refused; $try++) {
        if (-not $texts) { $texts = Get-TextStats $date $what }
        # on time: leave date blank (same as pressing the button; sheet refills from yesterday)
        $ghArgs = @("workflow", "run", "kpi-posts.yml", "--repo", $repo, "-f", "what=$what")
        if ($day.Date -ne (Get-Date).Date) { $ghArgs += @("-f", "date=$date") }
        if ($texts) { $ghArgs += @("-f", "texts=$texts") }
        # Stop mode turns gh's stderr into a crash that skips the log and the retry
        $ErrorActionPreference = "Continue"
        $ghOut = (& $gh @ghArgs 2>&1 | ForEach-Object { "$_" }) -join " "
        $ghRc = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($ghRc -eq 0) { $ok = $true }
        else {
            $ghError = $ghOut
            if ($ghOut -match "HTTP 4\d\d") { $refused = $true } else { Start-Sleep -Seconds 60 }
        }
    }
    if ($ok) {
        Log "started $what for $date$(if ($day.Date -ne $now.Date) { ' (catch-up)' })"
        # the post ran in GitHub's cloud, so remember its numbers on this PC for tomorrow's check
        if ($day.Date -eq $now.Date) {
            $ErrorActionPreference = "Continue"
            Push-Location $here
            $snapLog = Join-Path $here "reports\kpi_recheck.log"
            cmd /c "py -3 pull_kpis.py --from $date --to $date --snapshot-only >> `"$snapLog`" 2>&1"
            if ($what -eq "both") { cmd /c "py -3 pull_kpis.py --week-of $date --snapshot-only --note `"Weekly summary`" >> `"$snapLog`" 2>&1" }
            Pop-Location
            $ErrorActionPreference = "Stop"
        }
    } else {
        # GitHub would not start it: run the same steps on this PC instead
        Log "GitHub did not start $what for $date ($ghError); running on this PC"
        $ErrorActionPreference = "Continue"
        $localArgs = @{ Date = $date; What = $what; Texts = $texts }
        if ($day.Date -ne $now.Date) { $localArgs.CatchUp = $true }
        & (Join-Path $here "run_kpi_local.ps1") @localArgs
        $localRc = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($localRc -ne 0) {
            Log "FAILED: $what for $date on this PC (see reports\kpi_local_$date.log)"
            Alert-Once "KPI post for $date did not go out: GitHub refused it and the PC run failed. It retries every 15 minutes."
            exit 1
        }
        Log "posted $what for $date from this PC$(if ($day.Date -ne $now.Date) { ' (catch-up)' })"
    }
    $date | Out-File -Encoding ascii -NoNewline $state
    if ($due.Count -gt 1) { Start-Sleep -Seconds 90 }   # one run at a time
}
Run-Recheck
exit 0
