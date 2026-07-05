# OneCapture e-Claim - auto-start launcher.
# Brings up the FastAPI/uvicorn web server on http://127.0.0.1:8000.
# Registered as a Scheduled Task that runs at logon (see scripts/install-autostart.ps1).
# Waits for Postgres to be ready, refuses to double-start, and logs to logs/serve.log.
# NOTE: keep this file pure ASCII - Windows PowerShell 5.1 reads BOM-less .ps1 in the
# system codepage and mangles non-ASCII characters, which breaks parsing.

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot          # ...\onecapture
$Py   = Join-Path $Repo '.venv\Scripts\python.exe'
$Port = 8000
$LogDir = Join-Path $Repo 'logs'
$Log    = Join-Path $LogDir 'serve.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Note($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -FilePath $Log -Append -Encoding utf8 }

Set-Location $Repo

# If something is already serving the port, do nothing (avoids a second bind on a fast re-logon).
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Note "Port $Port already listening - launcher exiting, nothing to do."
    exit 0
}

# Wait for Postgres (port 5432) to accept connections - up to ~60s after a cold logon.
$ready = $false
foreach ($i in 1..60) {
    if (Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { Note "Postgres (5432) not up after 60s - starting uvicorn anyway." }
else { Note "Postgres is up." }

# Apply any pending DB migrations before serving, so a new revision (e.g. a new
# column) is in the schema by the time uvicorn handles its first request. Output
# is redirected at the OS level (Start-Process), NOT piped through the PowerShell
# pipeline - in PS 5.1 native stderr through the pipeline becomes NativeCommandError
# which, with ErrorActionPreference=Stop, would kill the launcher. A failed upgrade
# is logged but does not abort: the app still starts so the failure is visible.
$MigLog = Join-Path $LogDir 'alembic.log'
Note "Running alembic upgrade head"
$mig = Start-Process -FilePath $Py -ArgumentList '-m','alembic','upgrade','head' `
    -WorkingDirectory $Repo -NoNewWindow -PassThru -Wait `
    -RedirectStandardOutput $MigLog -RedirectStandardError "$MigLog.err"
if ($mig.ExitCode -ne 0) { Note "alembic upgrade FAILED (exit $($mig.ExitCode)) - see $MigLog.err; starting uvicorn anyway." }
else { Note "alembic upgrade head OK." }

# Supervise loop: keep uvicorn alive. Previously the launcher started uvicorn ONCE,
# logged its exit and returned 0 - so Task Scheduler saw the task "succeed" and never
# restarted until the next logon (the observed symptom: server dead all night, and
# its exits logged an EMPTY code = terminated externally, not a Python crash). Now we
# relaunch on every exit, with an exponential backoff so a genuine crash-loop (e.g. a
# bad migration) can't spin the CPU. A run that stayed up a while resets the backoff.
# No --reload: the reloader spawns a child that outlives task control and is flaky here.
# Start-Process redirects output at the OS level - piping uvicorn's stderr through the PS
# pipeline (or *>>) makes PS 5.1 wrap each log line as NativeCommandError and, with
# ErrorActionPreference=Stop, kill the server.
$OutLog = Join-Path $LogDir 'uvicorn.out.log'
$ErrLog = Join-Path $LogDir 'uvicorn.err.log'   # uvicorn logs here by default
$backoff = 2
$HEALTHY_SECS = 60      # a run this long is "healthy" -> reset backoff
$MAX_BACKOFF  = 60

while ($true) {
    Note "Starting uvicorn on http://127.0.0.1:$Port"
    $startedAt = Get-Date
    $proc = Start-Process -FilePath $Py `
        -ArgumentList '-m','uvicorn','eclaim.api.app:app','--host','127.0.0.1','--port',"$Port" `
        -WorkingDirectory $Repo -NoNewWindow -PassThru `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
    Note "uvicorn started, PID $($proc.Id)"
    $proc.WaitForExit()
    # $proc.ExitCode can be empty when the process was killed externally; guard the read.
    $code = try { $proc.ExitCode } catch { $null }
    $ranFor = [int]((Get-Date) - $startedAt).TotalSeconds
    Note "uvicorn exited (code '$code') after ${ranFor}s"

    # If another launcher (fast re-logon) has taken over the port, defer to it and stop.
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        Note "Port $Port already served by another instance - supervisor exiting."
        exit 0
    }

    if ($ranFor -ge $HEALTHY_SECS) { $backoff = 2 }                               # healthy -> reset
    else { $backoff = [Math]::Min($backoff * 2, $MAX_BACKOFF) }                   # crash-loop -> back off
    Note "Restarting uvicorn in ${backoff}s"
    Start-Sleep -Seconds $backoff
}
