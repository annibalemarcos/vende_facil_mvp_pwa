@echo off
mode con: cols=68 lines=14
cd /d "%~dp0"

echo ===============================================
echo          Vende Facil - MVP Local
echo ===============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente virtual...
    py -3 -m venv .venv
)

call .venv\Scripts\activate.bat

echo Instalando dependencias...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo.
echo Abrindo em http://127.0.0.1:5433
echo Para parar: CTRL + C
echo.
start "" http://127.0.0.1:5433
python app.py
pause
