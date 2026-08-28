@echo off
cd /d "%~dp0"
echo A iniciar a API da Agenda...
echo.
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if %ERRORLEVEL% EQU 0 (
    echo A porta 8000 ja esta a ser usada.
    echo.
    echo Experimenta abrir:
    echo http://127.0.0.1:8000/health
    echo.
    echo Se nao abrir, fecha outras janelas da API ou reinicia o PC.
    echo.
    pause
    exit /b 1
)

"..\TESTE_IP\runtime\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8000
echo.
echo A API foi encerrada.
pause
