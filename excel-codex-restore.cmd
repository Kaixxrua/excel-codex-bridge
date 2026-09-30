@echo off
rem Double-click: put Codex back on its own setup, as it was before the bridge.
rem The bridge's settings come out of %USERPROFILE%\.codex\config.toml (also ones
rem pasted in by hand), its own conversations move into Codex's list, and the
rem Windows timezone goes back. Nothing is downloaded.
if exist "%~dp0excel-codex.exe" goto exe
call "%~dp0excel-codex.cmd" restore %*
set "CODE=%errorlevel%"
rem excel-codex.cmd waits by itself when it fails.
if "%CODE%"=="0" pause
exit /b %CODE%
:exe
"%~dp0excel-codex.exe" restore %*
set "CODE=%errorlevel%"
pause
exit /b %CODE%
