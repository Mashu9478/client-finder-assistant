@echo off
setlocal
cd /d "%~dp0"
title Client Finder

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python is not installed on this computer.
  echo.
  echo 1. Download it from https://www.python.org/downloads/
  echo 2. In the installer, tick "Add python.exe to PATH".
  echo 3. Run this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up. This takes a minute or two...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, bs4, jinja2" >nul 2>nul
if errorlevel 1 (
  echo Installing what the app needs...
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto fail
)

".venv\Scripts\python.exe" run.py
if errorlevel 1 goto fail
exit /b 0

:fail
echo.
echo Something went wrong. Copy the text above and send it to Claude.
pause
exit /b 1
