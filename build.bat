@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller==6.22.3 || goto :err
python -m PyInstaller --onefile --console --name MM2Pricer mm2_pricer.py || goto :err
echo.
echo Готово: dist\MM2Pricer.exe
pause
exit /b 0
:err
echo.
echo Сборка не удалась, смотрите сообщения выше.
pause
exit /b 1
