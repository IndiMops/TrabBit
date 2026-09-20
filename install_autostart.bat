@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TrabBit.lnk"

if not exist "%PYTHONW%" (
    echo Не знайдено %PYTHONW%
    echo Спочатку створи .venv та виконай pip install -r requirements.txt
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath='%PYTHONW%'; $s.Arguments='main.py --tray'; $s.WorkingDirectory='%ROOT%'; $s.WindowStyle=7; $s.Description='TrabBit Toloka background manager'; $s.Save()"

if exist "%SHORTCUT%" (
    echo Автозапуск TrabBit встановлено.
    echo Shortcut: %SHORTCUT%
) else (
    echo Не вдалося створити ярлик автозапуску.
    exit /b 1
)
pause
endlocal
