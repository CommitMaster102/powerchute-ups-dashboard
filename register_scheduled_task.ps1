<#
    register_scheduled_task.ps1 - create the daily Windows Task (run once).

    Creates a real Task Scheduler job that runs scheduled_run.ps1 every day.
    Anti-infinite-scheduling: if a task of this name already exists, it does
    NOT create another - it reports and stops. Everything is in try/catch.

    Usage (no admin needed; it's a per-user task):
        powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1
        powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1 -RunTime 22:30
        powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1 -Force   # replace existing

    Remove it later with:
        Unregister-ScheduledTask -TaskName 'stateOfUPS Daily Analyzer' -Confirm:$false
#>

param(
    [string]$RunTime  = '09:00',                      # HH:mm, 24-hour
    [string]$TaskName = 'stateOfUPS Daily Analyzer',
    [switch]$Force                                    # replace an existing task
)

$ErrorActionPreference = 'Stop'

try {
    $root  = Split-Path -Parent $MyInvocation.MyCommand.Path
    $guard = Join-Path $root 'scheduled_run.ps1'
    if (-not (Test-Path $guard)) { throw "Guard script not found: $guard" }

    # Validate the time string up front.
    $parsedTime = [datetime]::MinValue
    if (-not [datetime]::TryParseExact($RunTime, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$parsedTime)) {
        throw "RunTime '$RunTime' is not valid HH:mm (e.g. 09:00 or 22:30)."
    }

    # --- Anti-duplicate guard: never create a second copy of the same task ---
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        if (-not $Force) {
            Write-Host "Task '$TaskName' already exists - not creating another."
            Write-Host "Re-run with -Force to replace it, or remove it with:"
            Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
            return
        }
        Write-Host "Task '$TaskName' exists - replacing it (-Force)."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $guard) `
        -WorkingDirectory $root

    $trigger = New-ScheduledTaskTrigger -Daily -At $RunTime

    # MultipleInstances IgnoreNew = OS-level collision guard (Task Scheduler will
    # not launch a second instance if one is still running). StartWhenAvailable =
    # if the machine was off at $RunTime, run at the next opportunity.
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings `
        -Description 'Daily PCSS UPS analyzer. Guarded: skips if already running or already ran today.' | Out-Null

    Write-Host "Created scheduled task '$TaskName' - runs daily at $RunTime."
    Write-Host "It invokes: $guard"
}
catch {
    Write-Error "Failed to register scheduled task: $($_.Exception.Message)"
    exit 1
}
