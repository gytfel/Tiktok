@echo off
chcp 65001 >nul
cd /d "%~dp0"
title TikTok Studio

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python не найден. Установите Python 3.10 или новее с https://www.python.org/downloads/
  echo и при установке отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

rem Окружение, зависимости и запуск готовит launcher.py.
%PY% launcher.py %*
pause
