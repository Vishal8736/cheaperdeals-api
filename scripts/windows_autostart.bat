@echo off
REM Starts Cheape Deal automatically every time you log in to Windows (a scheduled task). Run once.
REM To remove:  schtasks /Delete /TN CheapeDeal /F
set "HERE=%~dp0.."
schtasks /Create /SC ONLOGON /TN CheapeDeal /TR "cmd /c cd /d \"%HERE%\" && run_local.bat" /RL LIMITED /F
if errorlevel 1 (echo Could not create the task. Try again as Administrator.) else (echo Done. Cheape Deal will start when you log in.)
pause
