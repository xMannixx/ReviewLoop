@echo off
title ReviewLoop
chcp 65001 >nul

set PROJECT_DIR=%~dp0

cd /d "%PROJECT_DIR%"

if not exist "app.py" (
    echo.
    echo   FEHLER: app.py nicht gefunden in:
    echo   %PROJECT_DIR%
    pause
    exit /b 1
)

echo.
echo   *** ReviewLoop ***
echo.
echo   Browser oeffnet automatisch auf http://localhost:5000
echo   Stoppen: Fenster schliessen oder Ctrl+C
echo.

python app.py

pause
