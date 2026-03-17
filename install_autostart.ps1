param(
    [string]$AppDir = "$PSScriptRoot",
    [string]$TaskName = "BodycamUploader"
)

$ErrorActionPreference = "Stop"
$exe = Join-Path $AppDir "dist\BodycamUploader\BodycamUploader.exe"

if (-not (Test-Path $exe)) {
    Write-Host "Не найден $exe"
    Write-Host "Сначала соберите EXE через build_exe.bat."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $exe
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Автозапуск создан: $TaskName"
}
catch {
    Write-Host "Не удалось создать автозапуск: $($_.Exception.Message)"
    exit 1
}
