@echo off
setlocal EnableExtensions
title Stock Alert Widget Launcher

rem ---------------------------------------------------------------------------
rem 1. Resolve application directory (works beside or inside stock_alert_widget)
rem ---------------------------------------------------------------------------
set "APP=%~dp0"
if "%APP:~-1%"=="\" set "APP=%APP:~0,-1%"
if not exist "%APP%\main.py" (
  if exist "%APP%\stock_alert_widget\main.py" (
    set "APP=%APP%\stock_alert_widget"
  ) else (
    echo [ERROR] Could not find "main.py" in "%~dp0" or "%~dp0stock_alert_widget".
    echo Keep this .bat file either inside or right next to the stock_alert_widget folder.
    pause
    exit /b 1
  )
)
cd /d "%APP%" || (
  echo [ERROR] Could not change directory to "%APP%".
  pause
  exit /b 1
)

rem ---------------------------------------------------------------------------
rem 2. Parse subcommands and flags
rem ---------------------------------------------------------------------------
set "CMD=%~1"
set "FORCE_UPDATE=0"
set "IS_DEMO=0"

if /i "%CMD%"=="help" goto :show_help
if /i "%CMD%"=="--help" goto :show_help
if /i "%CMD%"=="-h" goto :show_help
if /i "%CMD%"=="/?" goto :show_help
if /i "%CMD%"=="logs" goto :open_logs
if /i "%CMD%"=="data" goto :open_logs
if /i "%CMD%"=="update" set "FORCE_UPDATE=1"

rem Detect if --mock or demo is requested anywhere in arguments
if /i "%CMD%"=="demo" set "IS_DEMO=1"
for %%A in (%*) do (
  if /i "%%~A"=="--mock" set "IS_DEMO=1"
)

rem ---------------------------------------------------------------------------
rem 3. Ensure virtual environment exists and is healthy
rem ---------------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" goto :create_venv
if not exist ".venv\Scripts\pythonw.exe" goto :create_venv
goto :venv_ready

:create_venv
if exist ".venv" (
  echo Cleaning up incomplete virtual environment...
  rmdir /s /q ".venv" >nul 2>&1
)
echo Creating virtual environment (first run only)...
for %%V in ("-3.12" "-3.13" "-3.14" "-3.11" "-3") do (
  if not exist ".venv\Scripts\pythonw.exe" (
    py %%~V -m venv .venv >nul 2>&1
  )
)
if not exist ".venv\Scripts\pythonw.exe" (
  python -m venv .venv >nul 2>&1
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo [ERROR] Could not create the virtual environment.
  echo Install 64-bit Python 3.12+ from https://www.python.org/ and ensure the Python Launcher ^(py^) is enabled.
  pause
  exit /b 1
)
set "FORCE_UPDATE=1"

:venv_ready

rem ---------------------------------------------------------------------------
rem 4. Install or update dependencies only when needed
rem ---------------------------------------------------------------------------
if not exist "requirements.txt" (
  echo [ERROR] Could not find "%APP%\requirements.txt".
  pause
  exit /b 1
)

set "NEED_PIP=%FORCE_UPDATE%"
if not exist ".venv\.installed_reqs" (
  set "NEED_PIP=1"
) else (
  fc /b "requirements.txt" ".venv\.installed_reqs" >nul 2>&1
  if errorlevel 1 set "NEED_PIP=1"
)

if "%NEED_PIP%"=="1" (
  echo Installing/updating packages from requirements.txt...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Package install failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
  copy /y "requirements.txt" ".venv\.installed_reqs" >nul 2>&1
)

if /i "%CMD%"=="update" (
  echo Packages are up to date.
  pause
  exit /b 0
)

rem ---------------------------------------------------------------------------
rem 5. Route tool subcommands (test, startup)
rem ---------------------------------------------------------------------------
if /i "%CMD%"=="test" (
  if "%~2"=="" (
    echo Running test suite...
    ".venv\Scripts\python.exe" -m pytest -q
  ) else (
    echo Running test suite with custom arguments...
    ".venv\Scripts\python.exe" -m pytest %2 %3 %4 %5 %6 %7 %8 %9
  )
  set "TEST_EXIT=%ERRORLEVEL%"
  pause
  exit /b %TEST_EXIT%
)

if /i "%CMD%"=="startup" (
  if "%~2"=="" (
    ".venv\Scripts\python.exe" "tools\startup_shortcut.py" status
  ) else (
    ".venv\Scripts\python.exe" "tools\startup_shortcut.py" %~2
  )
  set "STARTUP_EXIT=%ERRORLEVEL%"
  pause
  exit /b %STARTUP_EXIT%
)

rem ---------------------------------------------------------------------------
rem 6. Verify FINNHUB_API_KEY (live mode only; auto-load from User Registry)
rem ---------------------------------------------------------------------------
if "%IS_DEMO%"=="0" (
  if "%FINNHUB_API_KEY%"=="" (
    for /f "tokens=2,*" %%A in ('reg query "HKCU\Environment" /v FINNHUB_API_KEY 2^>nul ^| findstr /i "FINNHUB_API_KEY"') do set "FINNHUB_API_KEY=%%B"
  )
)

if "%IS_DEMO%"=="0" (
  if "%FINNHUB_API_KEY%"=="" (
    echo [WARNING] FINNHUB_API_KEY is not set. The widget will start but will not fetch live prices.
    echo Set it once in PowerShell:  setx FINNHUB_API_KEY "your-api-key"
    echo Or run demo mode instead:   %~nx0 demo
    pause
  )
)

rem ---------------------------------------------------------------------------
rem 7. Launch widget (demo, console/debug, or background pythonw)
rem ---------------------------------------------------------------------------
if /i "%CMD%"=="demo" (
  start "" ".venv\Scripts\pythonw.exe" "main.py" --mock %2 %3 %4 %5 %6 %7 %8 %9
  exit /b 0
)

if /i "%CMD%"=="debug" goto :run_console
if /i "%CMD%"=="console" goto :run_console

start "" ".venv\Scripts\pythonw.exe" "main.py" %*
exit /b 0

:run_console
echo Starting Stock Alert Widget in console mode (close window or press Ctrl+Alt+Shift+Q to quit)...
".venv\Scripts\python.exe" "main.py" %2 %3 %4 %5 %6 %7 %8 %9
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo [ERROR] Widget exited with code %APP_EXIT%.
  pause
)
exit /b %APP_EXIT%

:open_logs
set "DATA_DIR=%APPDATA%\StockAlertWidget"
if /i "%~2"=="demo" set "DATA_DIR=%APPDATA%\StockAlertWidget_demo"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%" >nul 2>&1
echo Opening "%DATA_DIR%"...
start "" explorer.exe "%DATA_DIR%"
exit /b 0

:show_help
echo Stock Alert Widget Launcher
echo.
echo Usage:
echo   %~nx0                         Start the widget in background (live mode)
echo   %~nx0 demo                    Start in demo mode (--mock, scripted prices, safe demo folder)
echo   %~nx0 debug [args]            Start in foreground console mode to view errors/tracebacks
echo   %~nx0 test [pytest-args]      Run the pytest suite (defaults to -q)
echo   %~nx0 startup [status^|create^|remove]
echo                                 Manage the Windows Startup login shortcut
echo   %~nx0 logs [demo]             Open %%APPDATA%%\StockAlertWidget in File Explorer
echo   %~nx0 update                  Force re-check/install packages from requirements.txt
echo   %~nx0 --data-dir PATH         Pass custom flags directly to main.py
echo   %~nx0 help                    Show this help message
exit /b 0
