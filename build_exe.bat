@echo off
rem Build StockChecker.exe on Windows (requires Python 3.10+)
cd /d %~dp0
python -m venv .venv || goto :err
call .venv\Scripts\activate.bat
pip install -r requirements.txt pyinstaller || goto :err
pyinstaller -y --clean StockChecker.spec || goto :err
echo.
echo Done: dist\StockChecker.exe
pause
exit /b 0
:err
echo Build failed
pause
exit /b 1
