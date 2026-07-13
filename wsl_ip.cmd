@echo off
REM ---------------------------------------------------------------------------
REM Print the WSL VM's IP address on stdout, nothing else.
REM
REM   wsl_ip.cmd            -> 172.x.x.x
REM
REM Pipe-friendly so you can capture it in another batch script:
REM
REM   for /f "delims=" %%I in ('wsl_ip.cmd') do set "WSL_IP=%%I"
REM
REM Same WSL-discovery recipe used by run.cmd's :launch path -- "wsl --
REM hostname -I" returns one or more space-separated addresses; we print
REM the first.
REM
REM Exit codes:
REM   0  -> printed an IP
REM   1  -> WSL is not installed / not running, or returned no address
REM ---------------------------------------------------------------------------
setlocal

set "WSL_IP="
for /f "tokens=1" %%I in ('wsl -- hostname -I 2^>nul') do (
    if not defined WSL_IP set "WSL_IP=%%I"
)
if not defined WSL_IP (
    echo [wsl_ip] ERROR: could not determine WSL IP from "wsl -- hostname -I". 1>&2
    echo         Is WSL installed, with a default distro running? 1>&2
    exit /b 1
)

echo %WSL_IP%
exit /b 0
