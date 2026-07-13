@echo off
REM ---------------------------------------------------------------------------
REM Run "clacogui_agent install" inside the project's venv.
REM
REM Examples:
REM     install_agent.cmd
REM     install_agent.cmd X:\.claude
REM     install_agent.cmd ftp://ftpuser:^<pw^>@host:2121/home/me/.claude
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo [install_agent] No virtual environment found at %VENV_PY%.
    echo                Run run.cmd at least once first to create it.
    exit /b 1
)

"%VENV_PY%" clacogui_agent.py install %*
exit /b %errorlevel%
