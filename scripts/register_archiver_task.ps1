<#
Creates or replaces the scheduled task that runs the ETF archiver twice daily; touches no other task.
Run: powershell -ExecutionPolicy Bypass -File <repo>\scripts\register_archiver_task.ps1
#>

$TaskName    = "PortfolioAnalytics_ETFArchiver"
$MorningRun  = "7:15AM"
$RetryRun    = "1:00PM"     # second chance: already-captured funds are skipped
$MakeDesktopShortcut = $true

$ErrorActionPreference = "Stop"

$Repo   = Split-Path -Parent $PSScriptRoot
$Config = Join-Path $Repo "archiver\config.ini"

# pythonw avoids a console window appearing at each scheduled run.
$Python = $null
if (Test-Path $Config) {
    $m = Select-String -Path $Config -Pattern '^\s*task_python\s*=\s*(.+?)\s*$' | Select-Object -First 1
    if ($m) { $Python = $m.Matches[0].Groups[1].Value }
}
if (-not $Python) {
    throw "Set task_python under [run] in $Config (see archiver\config.example.ini) - nothing was registered."
}
$Script = Join-Path $Repo "archiver\etf_archiver.py"

# Fail now rather than register a task that would fail silently every day.
foreach ($p in @($Python, $Script, $Repo)) {
    if (-not (Test-Path $p)) { throw "Not found: $p - nothing was registered." }
}

$action = New-ScheduledTaskAction -Execute $Python `
    -Argument "`"$Script`"" -WorkingDirectory $Repo

$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At $MorningRun),
    (New-ScheduledTaskTrigger -Daily -At $RetryRun)
)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
# A missed day cannot be recovered, so catch up after missed starts and do not
# let the battery defaults (skip on battery, stop when unplugged) drop a run.

# Interactive logon: no stored password, normal rights.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force `
    -Description "Saves daily ETF holdings snapshots. Missing a day is permanent. See $Repo\README.md" | Out-Null

# Report the settings as Task Scheduler stored them, not as requested.
$t = Get-ScheduledTask -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "Registered: $TaskName" -ForegroundColor Green
Write-Host ("  Runs          : daily at " + (($t.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString("h:mm tt") }) -join " and "))
Write-Host "  Program       : $($t.Actions[0].Execute) $($t.Actions[0].Arguments)"
Write-Host "  Start in      : $($t.Actions[0].WorkingDirectory)"
Write-Host "  On battery    : runs=$(-not $t.Settings.DisallowStartIfOnBatteries), keeps running=$(-not $t.Settings.StopIfGoingOnBatteries)"
Write-Host "  Wake to run   : $($t.Settings.WakeToRun)"
Write-Host "  Needs network : $($t.Settings.RunOnlyIfNetworkAvailable)"
Write-Host "  Catch up      : $($t.Settings.StartWhenAvailable)"
Write-Host "  Time limit    : $($t.Settings.ExecutionTimeLimit)   Overlap: $($t.Settings.MultipleInstances)"
Write-Host "  Next run      : $($i.NextRunTime)"

if ($MakeDesktopShortcut) {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desktop "ETF Archiver Status.lnk"
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $sh.TargetPath = "$env:WINDIR\notepad.exe"
    $sh.Arguments = "`"$Repo\ETF_ARCHIVER_STATUS.txt`""
    $sh.Description = "What the ETF archiver captured on its last run"
    $sh.Save()
    Write-Host "  Shortcut      : $lnk"
}

Write-Host ""
Write-Host "Note: 'wake to run' needs wake timers allowed in your power plan"
Write-Host "(Control Panel > Power Options > Change plan settings > Advanced >"
Write-Host "Sleep > Allow wake timers = Enable, for both battery and plugged in)."
