@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "TrabBit" ".venv\Scripts\pythonw.exe" "main.py" --tray
) else (
    echo Не знайдено .venv\Scripts\pythonw.exe
    echo Створи venv та встанови requirements.txt.
    pause
)
endlocal
