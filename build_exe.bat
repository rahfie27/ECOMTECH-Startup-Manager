@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

%PY% make_icon.py
if errorlevel 1 goto :error

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "ECOMTECH_Windows_Boot_Manager" ^
  --icon "assets\power.ico" ^
  --add-data "assets;assets" ^
  --uac-admin main.py
if errorlevel 1 goto :error

if exist "dist\ECOMTECH_Windows_Boot_Manager.exe" (
  echo.
  echo Build complete: dist\ECOMTECH_Windows_Boot_Manager.exe
  pause
  exit /b 0
)

echo.
echo Build command completed, but the EXE was not found.
pause
exit /b 1

:error
set "EXIT_CODE=%errorlevel%"
echo.
echo Build failed with error code %EXIT_CODE%. Review the output above.
pause
exit /b %EXIT_CODE%
