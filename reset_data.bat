@echo off
mode con: cols=68 lines=14
cd /d "%~dp0"

echo ===============================================
echo          Vende Facil - Zerar dados
echo ===============================================
echo.
echo Isto apaga o banco local, imagens importadas e o export JSON.
echo Use quando quiser recomecar do zero.
echo.
set /p CONFIRMAR="Digite ZERAR para confirmar: "
if /I not "%CONFIRMAR%"=="ZERAR" (
    echo Operacao cancelada.
    pause
    exit /b
)

if exist "data\vende_facil.sqlite" del /f /q "data\vende_facil.sqlite"
if exist "data\vende_facil_export.json" del /f /q "data\vende_facil_export.json"
if exist "data\uploads" rmdir /s /q "data\uploads"

echo.
echo Dados apagados. Rode run.bat para criar um banco novo vazio.
pause
