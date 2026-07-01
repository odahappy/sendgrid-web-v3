@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found. Run install_all_safe.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    ) else (
        echo ERROR: .env not found.
        pause
        exit /b 1
    )
)

echo Starting SendGrid Web Admin Scheduler...
echo.
.venv\Scripts\python.exe -m app.main

echo.
echo Service stopped.
pause
