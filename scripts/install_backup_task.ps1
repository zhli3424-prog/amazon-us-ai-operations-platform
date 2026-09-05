param(
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [string]$TaskName = "CrossBorderAI-DailyBackup",
  [string]$At = "02:30"
)

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$script = Join-Path $ProjectRoot "scripts\backup_db.py"
$database = Join-Path $ProjectRoot "data\app.db"
$target = Join-Path $ProjectRoot "backups"
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $script)) {
  throw "项目虚拟环境或备份脚本不存在"
}
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --source `"$database`" --target-dir `"$target`" --retention 14" -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "跨境智营台 SQLite 每日校验备份" -Force
Write-Output "已安装计划任务 $TaskName，每日 $At 执行"
