param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',

    [string]$AISFile = 'H:\全球数据\cleaned_v3\retime\2020-12-27_cleaned_retime.csv',

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

    [string]$SimulationId = '',

    [string]$BindAddress = '0.0.0.0',

    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectDirectory = Split-Path -Parent $PSScriptRoot
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
    $managedNames = @('daphne', 'ais-simulator') + (
        $DetectionFeatures | ForEach-Object { "detector-$_" }
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
if (-not (Test-Path -LiteralPath $AISFile -PathType Leaf)) {
    throw "AIS CSV file not found: $AISFile"
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

$browserAddress = Get-BrowserAddress
$predictUrl = "http://$browserAddress`:$Port/"
Write-Host ''
Write-Host "Predict AIS simulation: $predictUrl" -ForegroundColor Cyan
Write-Host "Source: $AISFile"
Write-Host "Mode: $Mode; speed factor: $SpeedFactor"
Write-Host "Simulation ID: $SimulationId"
Write-Host "Detection models: $StartDetectors"
Write-Host "Status: & '$PSCommandPath' status"
Write-Host "Stop: & '$PSCommandPath' stop"
Write-Host `
    "Logs: Get-Content '$LogDirectory\ais-simulator.out.log' -Wait"
