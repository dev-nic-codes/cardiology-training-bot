@echo off
setlocal
cd /d "%~dp0"
set PYTHONUNBUFFERED=1

if exist ".venv\Scripts\python.exe" goto run

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.12 -m venv .venv 2>nul
    if exist ".venv\Scripts\python.exe" goto run
    py -3 -m venv .venv 2>nul
    if exist ".venv\Scripts\python.exe" goto run
)

where python >nul 2>nul
if %errorlevel%==0 (
    python -m venv .venv
    if exist ".venv\Scripts\python.exe" goto run
)

echo Python 3.12+ was not found. Install Python and run this file again.
pause
exit /b 1

:run
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if %errorlevel% neq 0 goto fail

pip install -r requirements.txt
if %errorlevel% neq 0 goto fail

python main.py
set EXITCODE=%errorlevel%

echo.
echo Bot stopped with exit code %EXITCODE%.
pause
exit /b %EXITCODE%

:fail
echo.
echo Dependency installation failed.
pause
exit /b 1
