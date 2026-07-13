@echo off
REM ---------------------------------------------------------------------------
REM clacogui launcher.
REM
REM   - Creates .venv on first run.
REM   - Re-installs dependencies ONLY when requirements.txt changes
REM     (detected by SHA-256 hash stored in .venv\.req-stamp).
REM   - Otherwise just runs the app with the existing venv.
REM
REM Pass any extra args through to clacogui.py, e.g.:
REM     run.cmd X:\.claude
REM ---------------------------------------------------------------------------
setlocal

REM Always run from the script's own directory.
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "REQ_FILE=requirements.txt"
set "STAMP_FILE=%VENV_DIR%\.req-stamp"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM Pick a Python launcher for bootstrapping (creating the venv).
where py >nul 2>nul && (set "BOOT_PY=py -3") || (set "BOOT_PY=python")

REM ---------------------------------------------------------------------------
REM 1) Create the venv if it doesn't exist yet.
REM ---------------------------------------------------------------------------
if not exist "%VENV_PY%" (
    echo [clacogui] Creating virtual environment in %VENV_DIR% ...
    %BOOT_PY% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [clacogui] ERROR: failed to create virtual environment.
        echo            Is Python 3 installed and on PATH?
        exit /b 1
    )
    set "NEED_INSTALL=1"
)

REM ---------------------------------------------------------------------------
REM 2) Hash requirements.txt with the venv's own Python (stdlib only).
REM ---------------------------------------------------------------------------
set "REQ_HASH="
for /f "tokens=*" %%H in ('"%VENV_PY%" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "%REQ_FILE%" 2^>nul') do set "REQ_HASH=%%H"

if not defined REQ_HASH (
    echo [clacogui] WARNING: could not hash %REQ_FILE%; will reinstall to be safe.
    set "NEED_INSTALL=1"
)

REM ---------------------------------------------------------------------------
REM 3) Compare with the previous hash. Skip install if unchanged.
REM ---------------------------------------------------------------------------
set "OLD_HASH="
if exist "%STAMP_FILE%" set /p OLD_HASH=<"%STAMP_FILE%"

if not defined NEED_INSTALL if /i "%REQ_HASH%"=="%OLD_HASH%" goto launch

REM ---------------------------------------------------------------------------
REM 4) (Re)install dependencies into the venv.
REM ---------------------------------------------------------------------------
echo [clacogui] Installing dependencies from %REQ_FILE% ...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [clacogui] ERROR: failed to upgrade pip.
    exit /b 1
)
"%VENV_PY%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo [clacogui] ERROR: failed to install dependencies.
    exit /b 1
)

REM Record the new hash so the next launch can skip the install step.
> "%STAMP_FILE%" echo %REQ_HASH%

REM ---------------------------------------------------------------------------
REM 5) Launch the app.
REM
REM    Default spec: pyftpdlib running inside WSL on this box.  WSL has its
REM    own virtual NIC, so we ask it for its IP via "wsl -- hostname -I"
REM    (matching the recipe in ml_rename_inputs2.py).  Plain FTP avoids the
REM    Windows SMB metadata cache that polls otherwise miss.
REM
REM    Start the server inside WSL with the SAME password you set in
REM    CLACOGUI_FTP_PASSWORD (see below); e.g. if you exported
REM    CLACOGUI_FTP_PASSWORD=hunter2, run:
REM        python -m pyftpdlib -u ftpuser -P hunter2 \
REM            --range 60000-60009 -d / -w
REM
REM    Pass any path or URL on the command line to override, e.g.
REM        run.cmd X:\.claude
REM        run.cmd ftp://ftpuser:<pw>@host:2121/home/me/.claude
REM ---------------------------------------------------------------------------
:launch
if not "%~1"=="" goto run_with_args

REM Discover the WSL IP (first token of the first line of "hostname -I").
REM "if defined" is evaluated at runtime even inside the for body, so the
REM second iteration -- if there ever is one -- is correctly skipped without
REM needing delayed expansion.
set "WSL_IP="
for /f "tokens=1" %%I in ('wsl -- hostname -I 2^>nul') do (
    if not defined WSL_IP set "WSL_IP=%%I"
)
if not defined WSL_IP (
    echo [clacogui] ERROR: could not determine WSL IP from "wsl -- hostname -I".
    echo            Is WSL installed, with a default distro running?
    echo            Workaround: pass a spec on the command line, e.g.
    echo                run.cmd ftp://ftpuser:^<pw^>@host:2121/home/me/.claude
    exit /b 1
)
REM CLACOGUI_FTP_PASSWORD lets you keep the password out of scripts and out
REM of source control.  Set it once with `setx CLACOGUI_FTP_PASSWORD ...` (or
REM export it in your shell before invoking run.cmd) and start the WSL-side
REM `python -m pyftpdlib -u ftpuser -P <same-value> ...` with the identical
REM value.  If it isn't set, we fall back to the obviously-placeholder
REM ``changeme`` so the URL is still well-formed but authentication fails
REM loudly against any sensibly-configured server.
if not defined CLACOGUI_FTP_PASSWORD set "CLACOGUI_FTP_PASSWORD=changeme"
REM Likewise, the account on the WSL side may not match %USERNAME%.  Override
REM with CLACOGUI_WSL_HOME if it differs (must be an absolute path -- e.g.
REM /home/me/.claude).
if not defined CLACOGUI_WSL_HOME set "CLACOGUI_WSL_HOME=/home/%USERNAME%/.claude"
set "DEFAULT_SPEC=ftp://ftpuser:%CLACOGUI_FTP_PASSWORD%@%WSL_IP%:2121%CLACOGUI_WSL_HOME%"
echo [clacogui] Using FTP backend at %WSL_IP%:2121
"%VENV_PY%" clacogui.py "%DEFAULT_SPEC%"
exit /b %errorlevel%

:run_with_args
"%VENV_PY%" clacogui.py %*
exit /b %errorlevel%
