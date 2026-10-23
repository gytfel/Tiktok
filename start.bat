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

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: создаю окружение и устанавливаю зависимости. Это займёт пару минут...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Не удалось создать виртуальное окружение.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте подключение к интернету.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" run.py %*
pause
