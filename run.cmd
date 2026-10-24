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

REM CALL preserves the quoted executable path inside FOR /F's command shell.
for /f "tokens=*" %%H in ('call "%VENV_PY%" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "%REQ_FILE%" 2^>nul') do set "REQ_HASH=%%H"

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
REM Connect to the configured host's FTP port, including ports forwarded
REM by that host into WSL. No guest-IP discovery is needed.
REM Hidden-launcher profiles explicitly supply host, port, user and directories.
REM
REM You can still override the default target by passing a path or URL:
REM
REM     run.cmd X:\.claude
REM     run.cmd ftp://ftpuser:<pw>@somehost:2121/home/me/.claude
REM ---------------------------------------------------------------------------

:launch

if not "%~1"=="" goto run_with_args

REM Defaults for direct run.cmd use; hidden profiles override all these.
if not defined CLACOGUI_PROTOCOL set "CLACOGUI_PROTOCOL=ftp"
if not defined CLACOGUI_HOST set "CLACOGUI_HOST=%CLACOGUI_FTP_HOST%"
if not defined CLACOGUI_PORT set "CLACOGUI_PORT=%CLACOGUI_FTP_PORT%"
if not defined CLACOGUI_USER set "CLACOGUI_USER=%CLACOGUI_FTP_USER%"
if not defined CLACOGUI_HOST set "CLACOGUI_HOST=localhost"
if /i "%CLACOGUI_PROTOCOL%"=="sftp" goto configure_sftp
if /i not "%CLACOGUI_PROTOCOL%"=="ftp" (
    echo [clacogui] ERROR: CLACOGUI_PROTOCOL must be ftp or sftp.
    exit /b 1
)
if not defined CLACOGUI_PORT set "CLACOGUI_PORT=2121"
if not defined CLACOGUI_USER set "CLACOGUI_USER=ftpuser"
if not defined CLACOGUI_FTP_PASSWORD set "CLACOGUI_FTP_PASSWORD=changeme"
set "REMOTE_BASE=ftp://%CLACOGUI_USER%:%CLACOGUI_FTP_PASSWORD%@%CLACOGUI_HOST%:%CLACOGUI_PORT%"
goto configure_directories

:configure_sftp
if not defined CLACOGUI_PORT set "CLACOGUI_PORT=22"
if not defined CLACOGUI_USER set "CLACOGUI_USER=%USERNAME%"
set "REMOTE_BASE=sftp://%CLACOGUI_USER%@%CLACOGUI_HOST%:%CLACOGUI_PORT%"

:configure_directories
REM Accept the old directory variables for existing command-line setups.
if not defined CLACOGUI_CLAUDE_DIR set "CLACOGUI_CLAUDE_DIR=%CLACOGUI_WSL_HOME%"
if not defined CLACOGUI_CLAUDE_DIR set "CLACOGUI_CLAUDE_DIR=/home/%USERNAME%/.claude"
if not defined CLACOGUI_CODEX_DIR set "CLACOGUI_CODEX_DIR=%CLACOGUI_WSL_CODEX_HOME%"

set "DEFAULT_SPEC=%REMOTE_BASE%%CLACOGUI_CLAUDE_DIR%"

echo [clacogui] Using %CLACOGUI_PROTOCOL% backend at %CLACOGUI_HOST%:%CLACOGUI_PORT%
echo [clacogui] Claude directory: %CLACOGUI_CLAUDE_DIR%

if not defined CLACOGUI_CODEX_DIR goto run_claude_only

echo [clacogui] Codex directory: %CLACOGUI_CODEX_DIR%
"%VENV_PY%" clacogui.py "%DEFAULT_SPEC%" ^
    --codex "%REMOTE_BASE%%CLACOGUI_CODEX_DIR%"
exit /b %errorlevel%

:run_claude_only
"%VENV_PY%" clacogui.py "%DEFAULT_SPEC%"
exit /b %errorlevel%

REM ---------------------------------------------------------------------------
REM Explicit arguments supplied by caller: pass them straight through.
REM ---------------------------------------------------------------------------

:run_with_args

"%VENV_PY%" clacogui.py %*
exit /b %errorlevel%
