param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',

    [string]$AISFile = '',

    [ValidateSet('observed', 'broadcast', 'received')]
    [string]$Mode = 'received',

    [ValidateRange(0.01, 1000000)]
    [double]$SpeedFactor = 1,

    [ValidateRange(1, 1000000)]
    [int]$MaxShips = 200,

    [ValidateRange(0, 525600)]
    [double]$DurationMinutes = 60,

    [string]$SourceTimezone = 'Asia/Shanghai',

    [ValidateRange(1, 60000)]
    [double]$BatchMilliseconds = 1000,

    [ValidateRange(1, 1000000)]
    [int]$BatchSize = 1000,

    [ValidateRange(0, 2147483647)]
    [int]$MaxEvents = 0,

    [int]$Seed = 42,

    [switch]$NoWait,

    [switch]$KeepState,

    [bool]$StartDetectors = $true,

    # Run the JPDA AIS/Radar matcher and replay its paired scene data alongside
    # the normal Predict AIS simulator.  The matcher is loaded by this worker.
    [bool]$StartAISRadarReplay = $true,

    [string]$AISRadarDataDir = '',

    [string]$AISRadarScene = '08',

    [ValidateRange(0, 3600)]
    [double]$AISRadarInterval = 60,

    [bool]$AISRadarLoop = $true,

    [string]$SimulationId = '',

    [string]$BindAddress = '0.0.0.0',

    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$AISFile = if ($AISFile) {
    $AISFile
} else {
    Join-Path $ProjectDirectory 'Data\AIS\retime'
}
$AISRadarDataDir = if ($AISRadarDataDir) {
    $AISRadarDataDir
} else {
    Join-Path $ProjectDirectory 'Data\AIS-Rdar'
}
$RuntimeDirectory = Join-Path $ProjectDirectory '.runtime\ais-predict-demo'
$LogDirectory = Join-Path $RuntimeDirectory 'logs'
$Python = 'D:\Anaconda3\envs\Predict\python.exe'
$RedisServer = 'C:\Program Files\Redis\redis-server.exe'
$RedisCli = 'C:\Program Files\Redis\redis-cli.exe'
$SimulationIdFile = Join-Path $RuntimeDirectory 'simulation.id'
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
$FusionDetectionFeatures = @(
    'detect-ais-off',
    'detect-spoofing'
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

    $managedProcess = Get-Process `
        -Id $parsedProcessId `
        -ErrorAction SilentlyContinue
    if ($null -eq $managedProcess) {
        Remove-Item -LiteralPath $pidFile -Force
        return $null
    }
    return $managedProcess
}

function ConvertTo-CommandLineArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-ManagedProcess(
    [string]$Name,
    [string]$Executable,
    [string[]]$Arguments
) {
    $existingProcess = Get-ManagedProcess $Name
    if ($null -ne $existingProcess) {
        Write-Host `
            "[$Name] already running, PID=$($existingProcess.Id)" `
            -ForegroundColor Yellow
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
        Remove-Item `
            -LiteralPath (Get-PidFile $Name) `
            -Force `
            -ErrorAction SilentlyContinue
        if ($startedProcess.ExitCode -eq 0) {
            Write-Host "[$Name] completed successfully" -ForegroundColor Green
            return $null
        }
        throw "[$Name] failed to start. Check log: $stderrLog"
    }
    Write-Host `
        "[$Name] started, PID=$($startedProcess.Id)" `
        -ForegroundColor Green
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
    Remove-Item `
        -LiteralPath (Get-PidFile $Name) `
        -Force `
        -ErrorAction SilentlyContinue
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
    $testAddress = if ($Address -in @('0.0.0.0', '::')) {
        '127.0.0.1'
    } else {
        $Address
    }
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync($testAddress, $TcpPort)
        return $connectTask.Wait(300) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-BrowserAddress {
    if ($BindAddress -in @('0.0.0.0', '::')) {
        return '127.0.0.1'
    }
    return $BindAddress
}

function Show-Status {
    $redisStatus = if (Test-Redis) { 'running' } else { 'stopped' }
    Write-Host "[redis] $redisStatus"
    $managedNames = @(
        'daphne',
        'ais-simulator',
        'ais-radar-replay',
        'jpda-fusion'
    ) + (
        $DetectionFeatures | ForEach-Object { "detector-$_" }
    ) + (
        $FusionDetectionFeatures | ForEach-Object { "detector-$_" }
    )
    foreach ($processName in $managedNames) {
        $managedProcess = Get-ManagedProcess $processName
        if ($null -eq $managedProcess) {
            Write-Host "[$processName] not managed by this script"
        } else {
            Write-Host `
                "[$processName] running, PID=$($managedProcess.Id)"
        }
    }
    if (Test-Path -LiteralPath $SimulationIdFile) {
        $currentSimulationId = (
            Get-Content -LiteralPath $SimulationIdFile -Raw
        ).Trim()
        Write-Host "[simulation] id=$currentSimulationId"
    }
    if (Test-TcpPort $BindAddress $Port) {
        Write-Host "[web] port $Port is accepting connections"
    } else {
        Write-Host "[web] port $Port is not accepting connections"
    }
    Write-Host "Log directory: $LogDirectory"
}

if ($Action -eq 'status') {
    Show-Status
    exit 0
}

if ($Action -eq 'stop') {
    Stop-ManagedProcess 'ais-radar-replay'
    Stop-ManagedProcess 'jpda-fusion'
    foreach ($featureId in $FusionDetectionFeatures) {
        Stop-ManagedProcess "detector-$featureId"
    }
    Stop-ManagedProcess 'ais-simulator'
    foreach ($featureId in $DetectionFeatures) {
        Stop-ManagedProcess "detector-$featureId"
    }
    Stop-ManagedProcess 'daphne'
    if (
        (Test-Path -LiteralPath $Python) -and
        (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'manage.py')) -and
        (Test-Redis)
    ) {
        Write-Host '[detection] restoring operational AIS input'
        & $Python 'manage.py' 'activate_detection_source' `
            '--namespace' 'operational' `
            '--skip-checks'
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Could not restore the operational detection source.'
        }
    }
    Remove-Item `
        -LiteralPath $SimulationIdFile `
        -Force `
        -ErrorAction SilentlyContinue
    Write-Host '[redis] left running to avoid affecting other projects.'
    exit 0
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Predict environment Python not found: $Python"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'manage.py'))) {
    throw "Invalid project directory: $ProjectDirectory"
}
if (-not (Test-Path -LiteralPath $AISFile)) {
    throw "AIS CSV file or directory not found: $AISFile"
}
if ($StartAISRadarReplay -and -not (Test-Path -LiteralPath $AISRadarDataDir)) {
    throw "AIS/Radar replay data directory not found: $AISRadarDataDir"
}
if ($SimulationId -match ':') {
    throw "SimulationId cannot contain ':'."
}

Ensure-Redis

Write-Host '[django] applying database migrations'
& $Python 'manage.py' 'migrate' '--noinput'
if ($LASTEXITCODE -ne 0) {
    throw 'Django database migration failed; no process was started.'
}

Write-Host '[django] running project check'
& $Python 'manage.py' 'check'
if ($LASTEXITCODE -ne 0) {
    throw 'Django project check failed; no process was started.'
}

$managedDaphne = Get-ManagedProcess 'daphne'
if ($null -ne $managedDaphne) {
    Write-Host `
        "[daphne] already running, PID=$($managedDaphne.Id)" `
        -ForegroundColor Yellow
} elseif (Test-TcpPort $BindAddress $Port) {
    throw (
        "[daphne] port $Port is occupied by another service. " +
        'Stop the other launch profile before starting Predict.'
    )
} else {
    $previousAisDefaultSource = $env:AIS_DEFAULT_SOURCE
    try {
        $env:AIS_DEFAULT_SOURCE = 'predict'
        Start-ManagedProcess 'daphne' $Python @(
            '-u', '-m', 'daphne',
            '-b', $BindAddress,
            '-p', [string]$Port,
            'WanAna02702.asgi:application'
        ) | Out-Null
    } finally {
        if ($null -eq $previousAisDefaultSource) {
            Remove-Item Env:AIS_DEFAULT_SOURCE -ErrorAction SilentlyContinue
        } else {
            $env:AIS_DEFAULT_SOURCE = $previousAisDefaultSource
        }
    }
}

$existingSimulator = Get-ManagedProcess 'ais-simulator'
if ($null -ne $existingSimulator) {
    Write-Host `
        "[ais-simulator] already running, PID=$($existingSimulator.Id)" `
        -ForegroundColor Yellow
    Show-Status
    exit 0
}

$storedSimulationId = ''
if (Test-Path -LiteralPath $SimulationIdFile) {
    $storedSimulationId = (
        Get-Content -LiteralPath $SimulationIdFile -Raw
    ).Trim()
}
if (-not $SimulationId) {
    $SimulationId = if ($storedSimulationId) {
        $storedSimulationId
    } else {
        [guid]::NewGuid().ToString()
    }
} elseif ($storedSimulationId -and $storedSimulationId -ne $SimulationId) {
    $runningDetector = $DetectionFeatures | ForEach-Object {
        Get-ManagedProcess "detector-$_"
    } | Where-Object { $null -ne $_ } | Select-Object -First 1
    if ($null -ne $runningDetector) {
        throw (
            'A different simulation is still managed by this script. ' +
            'Run stop before changing -SimulationId.'
        )
    }
}
[System.IO.File]::WriteAllText(
    $SimulationIdFile,
    $SimulationId,
    [System.Text.UTF8Encoding]::new($false)
)

if ($StartAISRadarReplay) {
    Start-ManagedProcess 'jpda-fusion' $Python @(
        '-u', 'manage.py', 'jpda_fusion_worker', '--skip-checks'
    ) | Out-Null
}

if ($StartDetectors) {
    Write-Host "[detection] activating Predict source: $SimulationId"
    & $Python 'manage.py' 'activate_detection_source' `
        '--namespace' 'predict' `
        '--simulation-id' $SimulationId `
        '--force-reset' `
        '--skip-checks'
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not activate the Predict detection source.'
    }
    foreach ($featureId in $DetectionFeatures) {
        Start-ManagedProcess "detector-$featureId" $Python @(
            '-u', 'manage.py', 'detection_worker',
            '--detector', $featureId,
            '--namespace', 'predict',
            '--simulation-id', $SimulationId,
            '--skip-checks'
        ) | Out-Null
    }
    if ($StartAISRadarReplay) {
        foreach ($featureId in $FusionDetectionFeatures) {
            Start-ManagedProcess "detector-$featureId" $Python @(
                '-u', 'manage.py', 'fusion_detection_worker',
                '--detector', $featureId,
                '--skip-checks'
            ) | Out-Null
        }
    }
}

$invariant = [System.Globalization.CultureInfo]::InvariantCulture
$simulatorArguments = @(
    '-u', 'manage.py', 'simulate_ais_realtime',
    '--file', (ConvertTo-CommandLineArgument $AISFile),
    '--mode', $Mode,
    '--source-timezone', $SourceTimezone,
    '--speed-factor', $SpeedFactor.ToString($invariant),
    '--max-ships', [string]$MaxShips,
    '--duration-minutes', $DurationMinutes.ToString($invariant),
    '--batch-ms', $BatchMilliseconds.ToString($invariant),
    '--batch-size', [string]$BatchSize,
    '--seed', [string]$Seed,
    '--namespace', 'predict',
    '--simulation-id', $SimulationId,
    '--skip-checks'
)
if ($MaxEvents -gt 0) {
    $simulatorArguments += @('--max-events', [string]$MaxEvents)
}
if ($NoWait) {
    $simulatorArguments += '--no-wait'
}
if ($KeepState) {
    $simulatorArguments += '--keep-state'
}
if ($StartDetectors) {
    $simulatorArguments += '--enqueue-detections'
}

Start-ManagedProcess `
    'ais-simulator' `
    $Python `
    $simulatorArguments | Out-Null

if ($StartAISRadarReplay) {
    # Sensor playback is intentionally separate from JPDA and alert workers.
    $aisRadarReplayArguments = @(
        '-u', 'manage.py', 'ais_radar_replay',
        '--data-dir', (ConvertTo-CommandLineArgument $AISRadarDataDir),
        '--scene', $AISRadarScene,
        '--interval', $AISRadarInterval.ToString($invariant),
        '--skip-checks'
    )
    if ($AISRadarLoop) {
        $aisRadarReplayArguments += '--loop'
    }
    Start-ManagedProcess `
        'ais-radar-replay' `
        $Python `
        $aisRadarReplayArguments | Out-Null
}

$browserAddress = Get-BrowserAddress
$predictUrl = "http://$browserAddress`:$Port/"
Write-Host ''
Write-Host "Predict AIS simulation: $predictUrl" -ForegroundColor Cyan
Write-Host "Source: $AISFile"
Write-Host "Mode: $Mode; speed factor: $SpeedFactor"
Write-Host "AIS/Radar sensor replay: $StartAISRadarReplay; scene: $AISRadarScene; interval: $AISRadarInterval s"
Write-Host "JPDA fusion worker: $StartAISRadarReplay"
Write-Host "Simulation ID: $SimulationId"
Write-Host "Detection models: $StartDetectors"
Write-Host "Fusion alert models (CloseAIS/Forgery): $($StartDetectors -and $StartAISRadarReplay)"
Write-Host "Status: & '$PSCommandPath' status"
Write-Host "Stop: & '$PSCommandPath' stop"
Write-Host `
    "Logs: Get-Content '$LogDirectory\ais-simulator.out.log' -Wait"
