@echo off
cd /d "C:\Users\raymo\Downloads\engagemate"

:: Wait 120 seconds (2 minutes) for Wi-Fi/Hotspot to connect after waking from sleep
timeout /t 120 /nobreak

:: --- GATEKEEPER: ONLY RUN ONCE PER DAY ---
set "LAST_RUN_FILE=last_run_date.txt"
set "TODAY=%DATE%"

if exist "%LAST_RUN_FILE%" (
    set /p LAST_DATE=<"%LAST_RUN_FILE%"
) else (
    set "LAST_DATE=None"
)

if "%TODAY%"=="%LAST_DATE%" (
    echo [INFO] Already ran today: %TODAY%. Exiting.
    exit /b 0
)

echo %TODAY%>"%LAST_RUN_FILE%"
:: -----------------------------------------

echo Running Approved Fixes...
"C:\Python314\python.exe" -u apply_approved_fix.py 2>>autopilot_stderr.log
if errorlevel 1 echo [WARN] apply_approved_fix.py exited with error

echo Running Semantic Rule Updater...
"C:\Python314\python.exe" -u semantic_rule_updater.py 2>>autopilot_stderr.log
if errorlevel 1 echo [WARN] semantic_rule_updater.py exited with error

echo Running Auto Triage...
"C:\Python314\python.exe" -u auto_triage.py 2>>autopilot_stderr.log
if errorlevel 1 echo [WARN] auto_triage.py exited with error

exit /b 0