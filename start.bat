@echo off
rem ===================================================================
rem  rules_rag launcher - ASCII ONLY
rem  Do not put non-ASCII characters in this file.
rem  cmd.exe parses .bat as GBK on zh-CN Windows; a UTF-8 Chinese
rem  byte sequence shifts the read offset and swallows the next lines,
rem  which makes the script fail with "'xxx' is not recognized".
rem  All Chinese messages and all logic live in run.py.
rem ===================================================================
chcp 65001 >nul
cd /d "%~dp0"
title rules_rag

set "PY=D:\ana\.conda\envs\edu_rag\python.exe"
if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo         Edit start.bat and change the PY variable.
    pause
    exit /b 1
)
if not exist "run.py" (
    echo [ERROR] run.py not found. Keep start.bat and run.py together.
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

"%PY%" run.py
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [rules_rag] exited with code %RC%
    pause
)
exit /b %RC%
