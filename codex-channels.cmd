@echo off
setlocal
if exist "%~dp0excel-codex.exe" (
  "%~dp0excel-codex.exe" research %*
) else (
  call "%~dp0excel-codex.cmd" research %*
)
set "CHANNELS_EXIT=%errorlevel%"
if "%~1"=="" pause
exit /b %CHANNELS_EXIT%
