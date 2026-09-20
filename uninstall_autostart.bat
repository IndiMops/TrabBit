@echo off
chcp 65001 >nul
setlocal
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\TrabBit.lnk"
if exist "%SHORTCUT%" (
    del /q "%SHORTCUT%"
    echo Автозапуск TrabBit видалено.
) else (
    echo Ярлик автозапуску не знайдено.
)
pause
endlocal
