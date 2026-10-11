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
REM The FTP server runs inside WSL, but WSL exposes its listening ports to
REM Windows localhost. Therefore we do not need to discover the WSL VM IP.
REM
REM Start the FTP server inside WSL with the SAME password stored in
REM CLACOGUI_FTP_PASSWORD, for example:
REM
REM     python -m pyftpdlib -u ftpuser -P <password> ^
REM         --range 60000-60009 -d / -w
REM
REM The server listens on:
REM
REM     localhost:2121          FTP control connection
REM     localhost:60000-60009   passive FTP data connections
REM
REM You can still override the default target by passing a path or URL:
REM
REM     run.cmd X:\.claude
REM     run.cmd ftp://ftpuser:<pw>@somehost:2121/home/me/.claude
REM ---------------------------------------------------------------------------

:launch

if not "%~1"=="" goto run_with_args

REM Use Windows -> WSL localhost forwarding instead of resolving the
REM changing WSL virtual-machine IP address.
set "FTP_HOST=localhost"

REM CLACOGUI_FTP_PASSWORD should normally be supplied by run_hidden.vbs
REM or another environment variable.
if not defined CLACOGUI_FTP_PASSWORD set "CLACOGUI_FTP_PASSWORD=changeme"

REM Account/path inside WSL.
if not defined CLACOGUI_WSL_HOME set "CLACOGUI_WSL_HOME=/home/%USERNAME%/.claude"

REM Build the default Claude home FTP URL.
set "DEFAULT_SPEC=ftp://ftpuser:%CLACOGUI_FTP_PASSWORD%@%FTP_HOST%:2121%CLACOGUI_WSL_HOME%"

echo [clacogui] Using FTP backend at %FTP_HOST%:2121
echo [clacogui] Claude home: %CLACOGUI_WSL_HOME%

REM Optional Codex home over the same FTP server.
if defined CLACOGUI_WSL_CODEX_HOME (
    echo [clacogui] Codex home: %CLACOGUI_WSL_CODEX_HOME%

    "%VENV_PY%" clacogui.py "%DEFAULT_SPEC%" ^
        --codex "ftp://ftpuser:%CLACOGUI_FTP_PASSWORD%@%FTP_HOST%:2121%CLACOGUI_WSL_CODEX_HOME%"

    exit /b %errorlevel%
)

"%VENV_PY%" clacogui.py "%DEFAULT_SPEC%"
exit /b %errorlevel%

REM ---------------------------------------------------------------------------
REM Explicit arguments supplied by caller: pass them straight through.
REM ---------------------------------------------------------------------------

:run_with_args

"%VENV_PY%" clacogui.py %*
exit /b %errorlevel%
