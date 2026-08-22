param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',

    [string]$Scene = '08',

    [ValidateRange(0, 3600)]
    [double]$Interval = 60,

    [bool]$Loop = $true,

    [bool]$StartDetectors = $true,

    [bool]$StartVideoDetection = $true,

    [string]$CameraKey = 'harbor-01',

    [string]$BindAddress = '0.0.0.0',

    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectDirectory '.runtime\ais-radar-demo'
$LogDirectory = Join-Path $RuntimeDirectory 'logs'
$Python = 'D:\Anaconda3\envs\Predict\python.exe'
$RedisServer = 'C:\Program Files\Redis\redis-server.exe'
$RedisCli = 'C:\Program Files\Redis\redis-cli.exe'
$DetectionFeatures = @(
    'detect-CrossingBoundary',
    'detect-abnormalStaying',
    'detect-abnormalTransfer',
    'detect-abnormalWandering',
    'detect-blackList',
    'detect-collision',
    'detect-deviation',
    'detect-doubleDragging',
    'detect-highSpeedBoat',
    'detect-illegalAnchored',
    'detect-illegalBerthing',
    'detect-illegalStaying',
    'detect-lowSpeedBoat',
    'detect-smuggling'
)

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

function Get-PidFile([string]$Name) {
    return Join-Path $RuntimeDirectory "$Name.pid"
}

function Get-ManagedProcess([string]$Name) {
    $pidFile = Get-PidFile $Name
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }

    $rawProcessId = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $parsedProcessId = 0
    if (-not [int]::TryParse($rawProcessId, [ref]$parsedProcessId)) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }

    $managedProcess = Get-Process -Id $parsedProcessId -ErrorAction SilentlyContinue
    if ($null -eq $managedProcess) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }
    return $managedProcess
}

function Start-ManagedProcess(
    [string]$Name,
    [string]$Executable,
    [string[]]$Arguments
) {
    $existingProcess = Get-ManagedProcess $Name
    if ($null -ne $existingProcess) {
        Write-Host "[$Name] already running, PID=$($existingProcess.Id)" -ForegroundColor Yellow
        return $existingProcess
    }

    $stdoutLog = Join-Path $LogDirectory "$Name.out.log"
    $stderrLog = Join-Path $LogDirectory "$Name.err.log"
    $startedProcess = Start-Process `
        -FilePath $Executable `
        -ArgumentList $Arguments `
        -WorkingDirectory $ProjectDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    [System.IO.File]::WriteAllText(
        (Get-PidFile $Name),
        [string]$startedProcess.Id,
        [System.Text.UTF8Encoding]::new($false)
    )
    Start-Sleep -Milliseconds 1000
    $startedProcess.Refresh()
    if ($startedProcess.HasExited) {
        Remove-Item -LiteralPath (Get-PidFile $Name) -Force -ErrorAction SilentlyContinue
        throw "[$Name] failed to start. Check log: $stderrLog"
    }
    Write-Host "[$Name] started, PID=$($startedProcess.Id)" -ForegroundColor Green
    return $startedProcess
}

function Stop-ManagedProcess([string]$Name) {
    $managedProcess = Get-ManagedProcess $Name
    if ($null -eq $managedProcess) {
        Write-Host "[$Name] is not running"
        return
    }

    Stop-Process -Id $managedProcess.Id
    $managedProcess.WaitForExit(5000)
    Remove-Item -LiteralPath (Get-PidFile $Name) -Force -ErrorAction SilentlyContinue
    Write-Host "[$Name] stopped" -ForegroundColor Yellow
}

function Test-Redis {
    if (-not (Test-Path -LiteralPath $RedisCli)) {
        return $false
    }
    try {
        $reply = & $RedisCli ping 2>$null
        return $LASTEXITCODE -eq 0 -and ($reply -join '').Trim() -eq 'PONG'
    } catch {
        return $false
    }
}

function Ensure-Redis {
    if (Test-Redis) {
        Write-Host '[redis] already running' -ForegroundColor Green
        return
    }
    if (-not (Test-Path -LiteralPath $RedisServer)) {
        throw "Redis executable not found: $RedisServer"
    }

    Start-Process -FilePath $RedisServer -WindowStyle Hidden | Out-Null
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 500
        if (Test-Redis) {
            Write-Host '[redis] started' -ForegroundColor Green
            return
        }
    }
    throw 'Redis startup timed out. Check 127.0.0.1:6379.'
}

function Test-TcpPort([string]$Address, [int]$TcpPort) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync($Address, $TcpPort)
        return $connectTask.Wait(300) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Show-Status {
    $redisStatus = if (Test-Redis) { 'running' } else { 'stopped' }
    Write-Host "[redis] $redisStatus"
    $managedNames = @(
        'daphne',
        'ais-worker',
        'ais-radar-replay',
        'camera-stream',
        'overload-stream'
    ) + ($DetectionFeatures | ForEach-Object { "detector-$_" })
    foreach ($processName in $managedNames) {
        $managedProcess = Get-ManagedProcess $processName
        if ($null -eq $managedProcess) {
            Write-Host "[$processName] not managed by this script"
        } else {
            Write-Host "[$processName] running, PID=$($managedProcess.Id)"
        }
    }
    Write-Host "Log directory: $LogDirectory"
}

if ($Action -eq 'status') {
    Show-Status
    exit 0
}

if ($Action -eq 'stop') {
    Stop-ManagedProcess 'overload-stream'
    Stop-ManagedProcess 'camera-stream'
    Stop-ManagedProcess 'ais-radar-replay'
    Stop-ManagedProcess 'ais-worker'
    foreach ($featureId in $DetectionFeatures) {
        Stop-ManagedProcess "detector-$featureId"
    }
    Stop-ManagedProcess 'daphne'
    Write-Host '[redis] left running to avoid affecting other projects.'
    exit 0
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Predict environment Python not found: $Python"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'manage.py'))) {
    throw "Invalid project directory: $ProjectDirectory"
}

Ensure-Redis

Write-Host '[django] applying database migrations'
& $Python 'manage.py' 'migrate' '--noinput'
if ($LASTEXITCODE -ne 0) {
    throw 'Django database migration failed; no worker was started.'
}

Write-Host '[django] running one project check before starting workers'
& $Python 'manage.py' 'check'
if ($LASTEXITCODE -ne 0) {
    throw 'Django project check failed; no worker was started.'
}

$managedDaphne = Get-ManagedProcess 'daphne'
if ($null -eq $managedDaphne -and (Test-TcpPort $BindAddress $Port)) {
    throw "[daphne] $BindAddress`:$Port is occupied by a process not managed by this script. Stop it or choose another -Port."
} else {
    Start-ManagedProcess 'daphne' $Python @(
        '-u', '-m', 'daphne',
        '-b', $BindAddress,
        '-p', [string]$Port,
        'WanAna02702.asgi:application'
    ) | Out-Null
}

if ($StartDetectors) {
    foreach ($featureId in $DetectionFeatures) {
        Start-ManagedProcess "detector-$featureId" $Python @(
            '-u', 'manage.py', 'detection_worker',
            '--detector', $featureId,
            '--skip-checks'
        ) | Out-Null
    }
}

Start-ManagedProcess 'ais-worker' $Python @(
    '-u', 'manage.py', 'ais_worker', '--skip-checks'
) | Out-Null

$replayArguments = @(
    '-u', 'manage.py', 'ais_radar_replay',
    '--scene', $Scene,
    '--interval', ([string]::Format(
        [System.Globalization.CultureInfo]::InvariantCulture,
        '{0}',
        $Interval
    )),
    '--skip-checks'
)
if ($Loop) {
    $replayArguments += '--loop'
}
Start-ManagedProcess 'ais-radar-replay' $Python $replayArguments | Out-Null

if ($StartVideoDetection) {
    Start-ManagedProcess 'camera-stream' $Python @(
        '-u', 'manage.py', 'camera_stream_worker',
        '--camera-key', $CameraKey,
        '--skip-checks'
    ) | Out-Null
    Start-ManagedProcess 'overload-stream' $Python @(
        '-u', 'manage.py', 'overload_stream_worker',
        '--camera-key', $CameraKey,
        '--skip-checks'
    ) | Out-Null
}

Write-Host ''
Write-Host "All processes started: http://$BindAddress`:$Port/" -ForegroundColor Cyan
Write-Host "Status: & '$PSCommandPath' status"
Write-Host "Stop: & '$PSCommandPath' stop"
Write-Host "Logs: Get-Content '$LogDirectory\ais-radar-replay.out.log' -Wait"
