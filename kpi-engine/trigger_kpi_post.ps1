# Presses "Run workflow" on the GitHub "KPI posts" workflow for each weekday whose post time
# (7:00 AM the next weekday, since 2026-11-18) has passed and has not been posted yet.
# Run by the Windows task "KPI posts 7 AM weekdays".
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

# Jeff, 2026-11-17: this task runs from its own copy of main, so the SMS agent's live database
# and the shared smrtPhone session are named in kpi-engine\.env (never copied: a copy goes
# stale). Exported here so every step below, and run_kpi_local.ps1, sees them.
Get-Content (Join-Path $here ".env") -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^(KPI_SMS_DB|SMRTPHONE_STATE_FILE)=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        Set-Item "env:$k" $v.Trim().Trim('"').Trim("'")
    }

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
        $pyArgs = @((Join-Path $here "sms_kpis.py"), "--date", $date, "--b64-out", $b64)
        # no .venv in the KPI checkout: use py -3 like every other step (it has tzdata)
        if (-not (Test-Path $py)) { $py = "py"; $pyArgs = @("-3") + $pyArgs }
        if ($what -eq "both") { $pyArgs += "--week" }
        & $py @pyArgs *> (Join-Path $here "reports\texts_last.log")
        if (Test-Path $b64) { return (Get-Content $b64 -Raw).Trim() }
        Log "text stats for $date not worked out (see reports\texts_last.log)"
    } catch { Log "text stats for $date failed: $_" }
    return ""
}

# Jeff, 2026-11-18: a day now posts complete at 7 AM the next weekday, so the next-morning
# "Updated KPI's" check (kpi_recheck.py) is no longer run. This only archives the posts.
function Run-Recheck {
    $ErrorActionPreference = "Continue"
    # Jeff, 2026-11-16: keep every post outside Slack (free Slack drops them after 90 days)
    try { & (Join-Path $here "archive_kpi_posts.ps1") } catch { Log "post archive failed: $_" }
    $ErrorActionPreference = "Stop"
}

$now  = Get-Date
# Jeff, 2026-10-05: Oct 2 never posted. The PC slept through 6:30; a self-wake at 8:56 Sat
# started the missed run and sleep killed it. Jeff then used the PC 10:40-12:15 Sat, but
# unlocking is not a logon, so nothing retried until Monday. The task now also fires every
# 15 minutes and on wake/unlock; this script is safe to run any number of times.
$last = if (Test-Path $state) { [datetime]::ParseExact((Get-Content $state -Raw).Trim(), "yyyy-MM-dd", $null) }
        else { $now.Date.AddDays(-1) }

# Jeff, 2026-11-18: a weekday's KPIs post at 7:00 AM the NEXT weekday (Friday's, with the
# weekly summary and the sheet post, on Monday), so calls placed late in the day are in the
# post and no "Updated" post is needed. Was 6:30 PM the same day.
function PostTime($day) {
    $t = $day.Date.AddDays(1)
    while ($t.DayOfWeek -eq "Saturday" -or $t.DayOfWeek -eq "Sunday") { $t = $t.AddDays(1) }
    return $t.AddHours(7)
}

# every weekday after the last posted day whose 7 AM post time is already in the past (max 7 back)
$due = @()
$d = $last.AddDays(1)
if ($d -lt $now.Date.AddDays(-7)) { $d = $now.Date.AddDays(-7) }
while ($d -lt $now.Date) {
    if ($d.DayOfWeek -ne "Saturday" -and $d.DayOfWeek -ne "Sunday" -and $now -ge (PostTime $d)) {
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
        # the day posted is always an earlier day now, so name it
        $ghArgs = @("workflow", "run", "kpi-posts.yml", "--repo", $repo, "-f", "what=$what", "-f", "date=$date")
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
        Log "started $what for $date$(if ($now.Date -gt (PostTime $day).Date) { ' (catch-up)' })"
    } else {
        # GitHub would not start it: run the same steps on this PC instead
        Log "GitHub did not start $what for $date ($ghError); running on this PC"
        $ErrorActionPreference = "Continue"
        # CatchUp: refill the sheet from that day (it is always an earlier day now)
        $localArgs = @{ Date = $date; What = $what; Texts = $texts; CatchUp = $true }
        & (Join-Path $here "run_kpi_local.ps1") @localArgs
        $localRc = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($localRc -ne 0) {
            Log "FAILED: $what for $date on this PC (see reports\kpi_local_$date.log)"
            Alert-Once "KPI post for $date did not go out: GitHub refused it and the PC run failed. It retries every 15 minutes."
            exit 1
        }
        Log "posted $what for $date from this PC$(if ($now.Date -gt (PostTime $day).Date) { ' (catch-up)' })"
    }
    $date | Out-File -Encoding ascii -NoNewline $state
    if ($due.Count -gt 1) { Start-Sleep -Seconds 90 }   # one run at a time
}
Run-Recheck
exit 0
