@echo off
setlocal
cd /d "%~dp0"
title Update Client Finder

echo Close the Client Finder app and its black window before updating.
pause
echo.
echo Downloading the latest version...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $zip = Join-Path $env:TEMP 'cfa-update.zip'; $dir = Join-Path $env:TEMP 'cfa-update'; if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Mashu9478/client-finder-assistant/archive/refs/heads/main.zip' -OutFile $zip; Expand-Archive -Path $zip -DestinationPath $dir -Force; $src = Join-Path $dir 'client-finder-assistant-main'; Copy-Item -Path (Join-Path $src '*') -Destination (Get-Location).Path -Recurse -Force -Exclude 'update.bat','data'; Remove-Item $zip -Force; Remove-Item $dir -Recurse -Force"
if errorlevel 1 goto fail

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
)
echo.
echo Updated. Your leads and settings were not touched. Run start.bat to open the app.
pause
exit /b 0

:fail
echo.
echo The update did not finish. Copy the text above and send it to Claude.
pause
exit /b 1
