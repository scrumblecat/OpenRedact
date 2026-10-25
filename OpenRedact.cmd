@echo off
setlocal
python "%~dp0openredact.py" --keep-open %*
set "openredact_exit_code=%errorlevel%"
if "%openredact_exit_code%"=="9009" (
    echo(
    echo   Press any key to continue . . .
    pause >nul
)
exit /b %openredact_exit_code%
