@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo === Instalando el conector de Kommo para Claude ===
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo No se encontro Python. Instalalo desde https://www.python.org/downloads/
    echo y marca la opcion "Add python.exe to PATH" durante la instalacion.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creando el entorno de Python...
    py -3 -m venv .venv
    if errorlevel 1 goto error
)

echo Instalando dependencias (puede tardar un minuto)...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto error

echo Configurando la app Claude de escritorio...
".venv\Scripts\python.exe" configurar_claude_desktop.py
if errorlevel 1 goto error

echo.
echo === Listo ===
echo Cerra por completo la app Claude (clic derecho en su icono junto al reloj, "Quit" o "Salir")
echo y volve a abrirla.
echo.
pause
exit /b 0

:error
echo.
echo Hubo un error. Copia el mensaje de arriba y mandamelo.
pause
exit /b 1
