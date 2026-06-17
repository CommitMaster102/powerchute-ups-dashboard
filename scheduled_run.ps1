<#
    scheduled_run.ps1 - guarded daily run of the UPS analyzer.

    This is what the Windows Task Scheduler job invokes. It does NOT call
    run_analyzer.bat directly because that .bat ends in `pause` (which would
    hang an unattended task forever) and opens a browser. Instead it runs the
    same analyzer non-interactively: python analyze_ups.py --no-browser --quiet

    Guards:
      1. Collision  - if any analyzer run is already in progress (manual .bat
                      double-click or a previous scheduled run), skip.
      2. Once-a-day - if a scheduled run already completed today, skip.
      3. try/catch  - any error is logged and turned into a clean exit code;
                      a single mutex prevents two scheduled copies overlapping.

    Exit codes: 0 = ran or deliberately skipped, 1 = error.
    All activity is appended to output\scheduled_run.log.
#>

$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root 'output'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$runLog = Join-Path $logDir 'scheduled_run.log'
$marker = Join-Path $logDir 'last_scheduled_run.txt'

function Log([string]$msg) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $runLog -Value $line -ErrorAction Stop } catch { }
    Write-Host $line
}

# Single-instance lock so two *scheduled* copies can never overlap, even if
# Task Scheduler is misconfigured. Released in finally.
$mutex    = New-Object System.Threading.Mutex($false, 'Global\stateOfUPS-analyzer-scheduled')
$haveLock = $false

try {
    try {
        $haveLock = $mutex.WaitOne(0)   # non-blocking: take it or move on
    } catch [System.Threading.AbandonedMutexException] {
        # Previous owner crashed without releasing - we now own it. That's fine.
        $haveLock = $true
        Log 'NOTE: recovered an abandoned mutex from a prior crashed run.'
    }
    if (-not $haveLock) {
        Log 'SKIP: another scheduled analyzer run is already in progress.'
        exit 0
    }

    # --- Guard 1: is the analyzer already running anywhere (e.g. manual .bat)? ---
    $others = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match 'analyze_ups\.py' })
    if ($others.Count -gt 0) {
        Log ('SKIP: analyzer already running (PID {0}).' -f ($others.ProcessId -join ', '))
        exit 0
    }

    # --- Guard 2: did a scheduled run already finish today? ---
    $today = (Get-Date).ToString('yyyy-MM-dd')
    if (Test-Path $marker) {
        $last = ''
        try { $last = ((Get-Content -Path $marker -TotalCount 1 -ErrorAction Stop) | Out-String).Trim() } catch { }
        if ($last -eq $today) {
            Log "SKIP: analyzer already ran today ($today)."
            exit 0
        }
    }

    # --- Run it ---
    $py = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $py)) {
        Log "ERROR: venv python not found at $py - run setup first."
        exit 1
    }

    Log 'START: running analyzer (analyze_ups.py --no-browser --quiet).'
    & $py 'analyze_ups.py' '--no-browser' '--quiet' 2>&1 | ForEach-Object { Log "  $_" }
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        # Mark "ran today" only on success, so a failure retries on the next trigger.
        Set-Content -Path $marker -Value $today -ErrorAction SilentlyContinue
        Log 'DONE: analyzer completed successfully.'
    } else {
        Log "ERROR: analyzer exited with code $code (marker NOT updated)."
    }
    exit $code
}
catch {
    Log "EXCEPTION: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($haveLock) { try { $mutex.ReleaseMutex() } catch { } }
    $mutex.Dispose()
}
