@echo off
echo ============================================
echo  IxxatInterface v7.0 - Build
echo ============================================

echo Instalando dependencias...
pip install python-can PyQt5 pyqtgraph pyinstaller

echo.
echo Gerando executavel...
pyinstaller --onefile ^
    --windowed ^
    --name "IxxatInterface-v7" ^
    --add-data "core;core" ^
    --add-data "gui;gui" ^
    main.py

echo.
echo Build concluido! Executavel em: dist\IxxatInterface-v7.exe
pause
