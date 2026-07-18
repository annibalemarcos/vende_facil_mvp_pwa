@echo off
mode con: cols=68 lines=14
cd /d "%~dp0"

echo Instalando Vende Facil MVP...
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Pronto. Rode run.bat para iniciar.
pause
