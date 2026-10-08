@echo off
setlocal DisableDelayedExpansion
set "ENGINE=%~dp0ETL Engine"

REM Target Salesforce connection. Edit these two lines, then run this file.
REM The setup page does not ask for them; they are saved before the server starts.
set "TARGET_SF_LOGIN_URL=
set "TARGET_SF_SESSION_ID=

cd /d "%ENGINE%"

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Install Python 3.11 or newer and try again.
    pause
    exit /b 1
  )
  call ".venv\Scripts\python.exe" -m pip install --upgrade pip
  call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
)

if "%TARGET_SF_LOGIN_URL%"=="" (
  echo Set TARGET_SF_LOGIN_URL at the top of Open Setup.bat
  pause
  exit /b 1
)
if "%TARGET_SF_SESSION_ID%"=="" (
  echo Set TARGET_SF_SESSION_ID at the top of Open Setup.bat
  pause
  exit /b 1
)

echo Saving target Salesforce connection...
".venv\Scripts\python.exe" -c "import os; from core.workspace_layout import save_target_setup; save_target_setup(os.environ['TARGET_SF_LOGIN_URL'], os.environ['TARGET_SF_SESSION_ID'])"
if errorlevel 1 (
  echo Failed to save TARGET_SF_LOGIN_URL or TARGET_SF_SESSION_ID.
  pause
  exit /b 1
)

echo Stopping any server already using port 5000...
".venv\Scripts\python.exe" -c "from core.workspace_layout import stop_python_server; stop_python_server()"
ping 127.0.0.1 -n 3 >nul

echo Starting OrgShift setup at http://127.0.0.1:5000/
start "OrgShift ETL Engine" /D "%ENGINE%" cmd /k ".venv\Scripts\python.exe ui\app.py"
ping 127.0.0.1 -n 6 >nul
start "" http://127.0.0.1:5000/
endlocal
