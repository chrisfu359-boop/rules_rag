@echo off
rem ASCII only - see note in start.bat about GBK parsing of Chinese .bat files.
chcp 65001 >nul
echo [rules_rag] stopping service on port 8000 ...

set FOUND=0
rem Trailing space in ":8000 " keeps it from matching 18000 / 48000.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
    if not errorlevel 1 (
        echo   killed PID %%p
        set FOUND=1
    )
)

if "%FOUND%"=="0" echo   nothing was listening on port 8000
echo [rules_rag] done.
timeout /t 2 >nul
