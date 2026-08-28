@echo off
cd /d "%~dp0"
echo A iniciar a aplicacao Agenda...
echo.
"..\TESTE_IP\runtime\python.exe" app.py
echo.
echo A aplicacao foi encerrada.
pause
