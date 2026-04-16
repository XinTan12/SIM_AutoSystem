@echo off
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"
"%ROOT%.venv\Scripts\python.exe" "%ROOT%sim_control_app.py" --config "%ROOT%config\sim_control_config.json"
endlocal
