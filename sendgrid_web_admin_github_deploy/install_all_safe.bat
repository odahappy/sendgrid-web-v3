@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LOG=install_log.txt"

echo ==========================================
echo SendGrid Web Admin Scheduler - Installer
echo Python 3.9 compatible
echo ==========================================
echo.
echo Log file: %CD%\%LOG%
echo.

echo Install started at %DATE% %TIME% > "%LOG%"

echo Step 1/6: Checking Python...
where python >> "%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: python was not found in PATH.
    goto FAIL
)
python --version
python --version >> "%LOG%" 2>&1

echo Step 2/6: Creating .env...
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >> "%LOG%" 2>&1
        echo Created .env from .env.example.
    ) else (
        echo ERROR: .env.example not found.
        goto FAIL
    )
)

echo Step 3/6: Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        goto FAIL
    )
) else (
    echo Virtual environment already exists.
)

echo Step 4/6: Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: Failed to upgrade pip.
    goto FAIL
)

echo Trying Tsinghua mirror...
.venv\Scripts\pip.exe install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn >> "%LOG%" 2>&1
if errorlevel 1 (
    echo Tsinghua mirror failed. Trying Aliyun mirror...
    .venv\Scripts\pip.exe install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo ERROR: Failed to install dependencies.
        goto FAIL
    )
)

echo Step 5/6: Initializing database...
.venv\Scripts\python.exe -c "from app.db import init_db; init_db(); print('Database initialized.')" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo ERROR: Database initialization failed.
    goto FAIL
)

echo Step 6/6: Done.
echo.
echo Installation completed successfully.
echo Next:
echo 1. Edit .env and change ADMIN_PASSWORD, SECRET_KEY, SERVICE_TOKEN.
echo 2. Double click start_web_admin.bat.
echo.
pause
exit /b 0

:FAIL
echo.
echo Installation failed. Please open install_log.txt.
pause
exit /b 1
