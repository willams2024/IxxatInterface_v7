@echo off
chcp 65001 >nul
cd /d "%~dp0"
python main.py
if errorlevel 1 (
    echo.
    echo ERRO ao iniciar. Verifique se as dependencias estao instaladas:
    echo pip install PyQt5 python-can
    pause
)
