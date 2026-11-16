# Copies the exact text of every KPI post into the Stonebridge OneDrive, because free
# Slack drops messages after 90 days (Jeff, 2026-11-16; he does not want these on his
# personal OneDrive). Called by trigger_kpi_post.ps1 every time it runs (every 15 min).
#
# Posts made on GitHub are attached to their run as the artifact "kpi-posts-<run id>"
# (see .github/workflows/kpi-posts.yml); this downloads each finished run's artifact
# once. Posts made on this PC (the next-morning "Updated KPI's" check, or a run GitHub
# refused) are written straight into the folder by pull_kpis.py / kpi_slack_bot.py.
#
# Folder: KPI_POST_ARCHIVE in kpi-engine\.env. Not set = do nothing.
# State:  reports\kpi_posts_archived.txt (run ids already handled).
# A failure here never blocks a post; a run that fails to download is retried next time.

$ErrorActionPreference = "Continue"
$gh   = "C:\Program Files\GitHub CLI\gh.exe"
$repo = "dsongstonebridge/SiftStack"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $here "reports\kpi_post_trigger.log"
$done = Join-Path $here "reports\kpi_posts_archived.txt"

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  archive: $m" | Out-File -Append -Encoding utf8 $log }

$line = Get-Content (Join-Path $here ".env") -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^KPI_POST_ARCHIVE=' } | Select-Object -First 1
$dest = ($line -replace '^KPI_POST_ARCHIVE=', '').Trim().Trim('"').Trim("'")
if (-not $dest) { exit 0 }
New-Item -ItemType Directory -Force $dest | Out-Null

$handled = @{}
if (Test-Path $done) { Get-Content $done | ForEach-Object { $handled[$_.Trim()] = $true } }

$json = & $gh run list --repo $repo --workflow kpi-posts.yml --limit 20 --json databaseId,status 2>&1
if ($LASTEXITCODE -ne 0) { Log "could not list runs: $json"; exit 0 }
# Not piped: Windows PowerShell 5.1 passes a piped JSON array on as ONE object, which
# glued all 20 run ids into a single request.
$runs = ConvertFrom-Json ($json -join "")

foreach ($r in $runs) {
    $id = "$($r.databaseId)"
    if ($r.status -ne "completed" -or $handled.ContainsKey($id)) { continue }
    $tmp = Join-Path $env:TEMP "kpi-posts-$id"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $out = (& $gh run download $id --repo $repo --name "kpi-posts-$id" --dir $tmp 2>&1 | ForEach-Object { "$_" }) -join " "
    if ($LASTEXITCODE -ne 0) {
        if ($out -match 'no (valid )?artifacts?') {
            # A run with nothing posted, or one from before this was built: nothing to keep.
            $id | Out-File -Append -Encoding ascii $done
        } else {
            Log "run $id not archived yet (will retry): $out"
        }
        continue
    }
    $files = @(Get-ChildItem -Recurse -File $tmp -Filter *.txt)
    foreach ($f in $files) { Copy-Item $f.FullName (Join-Path $dest $f.Name) -Force }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    $id | Out-File -Append -Encoding ascii $done
    if ($files.Count) { Log "run ${id}: saved $($files.Count) post(s) to $dest" }
}
exit 0
