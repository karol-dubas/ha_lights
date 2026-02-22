$pcListenerDir = $PSScriptRoot
$repoRoot = Split-Path $pcListenerDir -Parent
$pythonExe = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$scriptPath = Join-Path $pcListenerDir "pc_listener.py"

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument $scriptPath `
    -WorkingDirectory $repoRoot

$trigger = New-ScheduledTaskTrigger -AtLogon

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName "MonitorMQTTListener" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "MQTT listener for monitor brightness/contrast" -Force