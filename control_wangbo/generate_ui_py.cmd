@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI\"
pushd "%PROJECT_ROOT%" >nul

set "PYTHON_EXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found: "%PYTHON_EXE%"
    popd >nul
    exit /b 1
)

if "%~1"=="" (
    set "UI_FILE=%SCRIPT_DIR%CellSorting_ui.ui"
) else (
    set "UI_FILE=%~f1"
)

if not exist "%UI_FILE%" (
    echo [ERROR] UI file not found: "%UI_FILE%"
    popd >nul
    exit /b 1
)

for %%I in ("%UI_FILE%") do (
    set "UI_DIR=%%~dpI"
    set "UI_NAME=%%~nI"
)

set "OUT_FILE=!UI_DIR!!UI_NAME!.py"

echo [INFO] UI file : "%UI_FILE%"
echo [INFO] Output  : "!OUT_FILE!"
"%PYTHON_EXE%" -m PyQt5.uic.pyuic "%UI_FILE%" -o "!OUT_FILE!"

if errorlevel 1 (
    echo [ERROR] Failed to generate Python file.
    popd >nul
    exit /b 1
)

echo [OK] Generated: "!OUT_FILE!"
popd >nul
endlocal
