param([switch]$RunPickupDaily)

$ErrorActionPreference = 'Stop'
$worker = Split-Path $PSScriptRoot -Parent
$python = (Resolve-Path -LiteralPath (Join-Path $worker '.venv\Scripts\python.exe')).Path
$node = (Get-Command node.exe).Source
$vietnam = [TimeZoneInfo]::FindSystemTimeZoneById('SE Asia Standard Time')
$now = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::Now, $vietnam)
$null = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'node_modules\wrangler\bin\wrangler.js')

if ($RunPickupDaily) {
    $day = $now.Date.AddDays(-1).ToString('yyyy-MM-dd')
    $snapshot = Join-Path $worker "data\pickup-daily\$day.json"
    $observed = if (Test-Path -LiteralPath $snapshot) {
        [DateTimeOffset]::Parse((Get-Content -LiteralPath $snapshot -Encoding utf8 -Raw | ConvertFrom-Json).observed_at)
    }
    if (!$observed -or $observed.UtcDateTime -lt [TimeZoneInfo]::ConvertTimeToUtc($now.Date, $vietnam)) {
        & $python -X utf8 (Join-Path $PSScriptRoot 'pickup-daily-orders.py') --date $day
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $node (Join-Path $PSScriptRoot 'archive-pickup-daily.mjs') $day
    exit $LASTEXITCODE
}

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit (New-TimeSpan -Minutes 40)

function DailyTrigger([int]$hour, [int]$minute) {
    $next = $now.Date.AddHours($hour).AddMinutes($minute)
    if ($next -le $now.DateTime) { $next = $next.AddDays(1) }
    $utc = [TimeZoneInfo]::ConvertTimeToUtc($next, $vietnam)
    $trigger = New-ScheduledTaskTrigger -Daily -At $utc.ToLocalTime()
    $trigger.StartBoundary = $utc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
    return $trigger
}

function PythonAction([string]$file, [string]$arguments) {
    $script = Join-Path $PSScriptRoot $file
    return New-ScheduledTaskAction -Execute $python -WorkingDirectory $worker `
        -Argument ('-X utf8 "{0}" {1}' -f $script, $arguments)
}

foreach ($slot in @(@{Cot='COT1'; Hour=13}, @{Cot='COT2'; Hour=17})) {
    Register-ScheduledTask -TaskName ("SDD Pickup On Hold " + $slot.Cot) -Trigger (DailyTrigger $slot.Hour 0) `
        -Action (PythonAction 'pickup-onhold-report.py' ("--cot " + $slot.Cot)) -Settings $settings -Force
}

$archive = New-ScheduledTaskAction -Execute (Join-Path $PSHOME 'powershell.exe') -WorkingDirectory $worker `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -RunPickupDaily' -f $PSCommandPath)
Register-ScheduledTask -TaskName 'SDD Pickup Daily Cloudflare Archive' -Trigger (DailyTrigger 0 15) `
    -Action $archive -Settings $settings -Force

Register-ScheduledTask -TaskName 'SDD Delivery Backlog Measurements' -Trigger (DailyTrigger 17 45) `
    -Action (PythonAction 'delivery-backlog-measurements.py' '--only history') -Settings $settings -Force

$liveStart = $now.Date.AddMinutes((1 + [Math]::Floor($now.TimeOfDay.TotalMinutes / 10)) * 10)
$liveUtc = [TimeZoneInfo]::ConvertTimeToUtc($liveStart, $vietnam)
$live = New-ScheduledTaskTrigger -Once -At $liveUtc.ToLocalTime() -RepetitionInterval (New-TimeSpan -Minutes 10)
$live.StartBoundary = $liveUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
Register-ScheduledTask -TaskName 'SDD Delivery Live Measurements' -Trigger $live `
    -Action (PythonAction 'delivery-backlog-measurements.py' '--only live --scheduled-live') -Settings $settings -Force
