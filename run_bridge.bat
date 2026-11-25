@echo off
REM Start the MT5 bridge. Open and log in to the MT5 terminal first.
cd /d "%~dp0"
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
python -m mt5_bridge
pause
