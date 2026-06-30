# Registers (or refreshes) the "OneCapture eClaim" Scheduled Task so the web server
# auto-starts at logon. Re-run this any time to update settings. Idempotent.
# To remove: Unregister-ScheduledTask -TaskName 'OneCapture eClaim' -Confirm:$false

$ErrorActionPreference = 'Stop'
$Repo    = Split-Path -Parent $PSScriptRoot
$Serve   = Join-Path $Repo 'scripts\serve.ps1'
$TaskName = 'OneCapture eClaim'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Serve`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Run as the current user, in their own session (no stored password, no admin needed).
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

# Long-running server: no time limit, restart up to 3x if it dies, keep running on battery.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'Auto-starts the OneCapture e-Claim web server (uvicorn) at logon.' `
    -Force | Out-Null

Write-Host "Registered scheduled task: '$TaskName' (runs at logon)."
