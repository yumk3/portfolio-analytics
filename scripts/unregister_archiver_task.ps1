<#
Removes the ETF archiver's scheduled task only; captured data, logs and backups are untouched.
Run: powershell -ExecutionPolicy Bypass -File <repo>\scripts\unregister_archiver_task.ps1
To pause instead, use Disable-ScheduledTask / Enable-ScheduledTask on the same task name.
#>

$TaskName = "PortfolioAnalytics_ETFArchiver"

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "No task named $TaskName - nothing to remove."
    exit 0
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Yellow
Write-Host "Your captured files and logs are untouched. The desktop shortcut"
Write-Host "'ETF Archiver Status' (if any) can be deleted by hand."
