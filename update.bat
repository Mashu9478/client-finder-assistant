@echo off
setlocal
cd /d "%~dp0"
title Update Client Finder

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed, so this file cannot fetch updates.
  echo Either install Git from https://git-scm.com/download/win and run this again,
  echo or download the latest ZIP from GitHub and copy your "data" folder into it.
  pause
  exit /b 1
)
if not exist ".git" (
  echo This folder was not downloaded with Git, so it cannot update itself.
  echo Download the latest ZIP from GitHub and copy your "data" folder into it.
  pause
  exit /b 1
)

echo Getting the latest version...
git pull --ff-only
if errorlevel 1 goto fail

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto fail
)
echo.
echo Updated. Close the app if it is open, then run start.bat.
pause
exit /b 0

:fail
echo.
echo The update did not finish. Copy the text above and send it to Claude.
pause
exit /b 1
