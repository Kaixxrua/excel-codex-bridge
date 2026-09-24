@echo off
rem Double-click: the Codex desktop app / IDE extension use the Excel bridge
rem while this window stays open; closing it puts your Codex config back.
if exist "%~dp0excel-codex.exe" goto exe
call "%~dp0excel-codex.cmd" desktop %*
exit /b %errorlevel%
:exe
"%~dp0excel-codex.exe" desktop %*
if errorlevel 1 pause
exit /b %errorlevel%
