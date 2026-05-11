@echo off
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"
"%ROOT%.venv\Scripts\python.exe" "%ROOT%app.py"
endlocal
