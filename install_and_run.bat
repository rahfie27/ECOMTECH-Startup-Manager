@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% -m pip install --upgrade pip
if errorlevel 1 goto :error

%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :error

%PY% main.py
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" goto :error_code
exit /b 0

:error
set "EXIT_CODE=%errorlevel%"

:error_code
echo.
echo Installation or application launch failed with error code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
