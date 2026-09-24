@echo off
rem excel-codex: run Codex on your own ChatGPT Excel add-in session.
rem   From a project folder:  excel-codex [options] [-- codex arguments]
rem   Double-click:           Codex starts in your user folder.
rem First run creates .venv next to this file and installs the dependencies.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "DOUBLECLICK="

rem Started from Explorer: keep Codex out of this folder.
if /i "%CD%\"=="%ROOT%" (
  set "DOUBLECLICK=1"
  cd /d "%USERPROFILE%"
)

if not exist "%VPY%" call :create_venv || goto :failed
fc /b "%ROOT%requirements.txt" "%VENV%\requirements.installed" >nul 2>nul || call :install || goto :failed

set "SRC=%ROOT%src"
if defined PYTHONPATH set "SRC=%SRC%;%PYTHONPATH%"
set "PYTHONPATH=%SRC%"
"%VPY%" -m excel_codex_bridge %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" if defined DOUBLECLICK pause
exit /b %CODE%

:create_venv
echo [excel-codex] First run: creating a Python environment in "%VENV%" ...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul || goto :no_python
%PY% -m venv "%VENV%" || exit /b 1
exit /b 0

:no_python
echo [excel-codex] Python 3.10 or newer was not found.
echo   Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo   then run this again.
exit /b 1

:install
echo [excel-codex] Installing dependencies ...
"%VPY%" -m pip install --disable-pip-version-check -q -r "%ROOT%requirements.txt" || exit /b 1
copy /y "%ROOT%requirements.txt" "%VENV%\requirements.installed" >nul
exit /b 0

:failed
echo.
echo [excel-codex] Setup failed. If pip cannot reach PyPI, choose a mirror first, e.g.
echo   set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
echo and run this again. To start over, delete the .venv folder next to this file.
pause
exit /b 1
