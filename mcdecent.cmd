@echo off
rem mcdecent entry (Windows).
rem   run "mcdecent"                       -> interactive shell (type commands)
rem   run "mcdecent launch"                -> launch the game in foreground
rem   run "mcdecent help"                  -> list all commands
setlocal
set "MCDECENT_ROOT=%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [mcdecent] python not found. Install Python 3.10+ first.
  exit /b 1
)
set "PYTHONPATH=%MCDECENT_ROOT%;%PYTHONPATH%"
python -m mcdecent %*
exit /b %ERRORLEVEL%
