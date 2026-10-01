@echo off
setlocal
cd /d "%~dp0"
py -m pip install -r requirements.txt
py -m pip install pyinstaller
pyinstaller --noconsole --onefile --icon=app.ico --name StartupManager startup_manager.py
endlocal
