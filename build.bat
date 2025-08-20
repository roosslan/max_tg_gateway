@echo off
REM Собирает main.py в один exe-файл без консольного окна.
REM Требуется: pip install -r requirements.txt

pyinstaller --onefile --noconsole --name max_tg_gateway --icon assets\icon.ico main.py

echo.
echo Готово. Файл: dist\max_tg_gateway.exe
echo Файл ini.toml необходимо положить рядом с exe перед запуском.
