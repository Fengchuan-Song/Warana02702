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

    [string]$BindAddress = '0.0.0.0',

    [ValidateRange(1, 65535)]
    [int]$Port = 8001
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectDirectory = Split-Path -Parent $PSScriptRoot
$RuntimeDirectory = Join-Path $ProjectDirectory '.runtime\ais-predict-demo'
$LogDirectory = Join-Path $RuntimeDirectory 'logs'
$Python = 'D:\Anaconda3\envs\Predict\python.exe'
$RedisServer = 'C:\Program Files\Redis\redis-server.exe'
$RedisCli = 'C:\Program Files\Redis\redis-cli.exe'

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
    foreach ($processName in @('daphne', 'ais-simulator')) {
        $managedProcess = Get-ManagedProcess $processName
        if ($null -eq $managedProcess) {
            Write-Host "[$processName] not managed by this script"
        } else {
            Write-Host `
                "[$processName] running, PID=$($managedProcess.Id)"
        }
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
if (-not (Test-Path -LiteralPath $AISFile -PathType Leaf)) {
    throw "AIS CSV file not found: $AISFile"
}

Ensure-Redis

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
    Write-Host `
        "[daphne] reusing the existing service on port $Port; it is not managed by this script" `
        -ForegroundColor Yellow
    Write-Host `
        '[daphne] the existing process must already include the /ws/ais-predict/ route' `
        -ForegroundColor Yellow
} else {
    Start-ManagedProcess 'daphne' $Python @(
        '-u', '-m', 'daphne',
        '-b', $BindAddress,
        '-p', [string]$Port,
        'WanAna02702.asgi:application'
    ) | Out-Null
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

Start-ManagedProcess `
    'ais-simulator' `
    $Python `
    $simulatorArguments | Out-Null

$browserAddress = Get-BrowserAddress
$predictUrl = "http://$browserAddress`:$Port/?ais_source=predict"
Write-Host ''
Write-Host "Predict AIS simulation: $predictUrl" -ForegroundColor Cyan
Write-Host "Source: $AISFile"
Write-Host "Mode: $Mode; speed factor: $SpeedFactor"
Write-Host "Status: & '$PSCommandPath' status"
Write-Host "Stop: & '$PSCommandPath' stop"
Write-Host `
    "Logs: Get-Content '$LogDirectory\ais-simulator.out.log' -Wait"
