@echo off
REM Double-click me to pull fresh prices and rebuild the site.
REM Windows. Requires Python 3 (https://www.python.org/downloads/).
setlocal

cd /d "%~dp0"

REM Pick python vs python3 vs py -3 — whichever is on PATH.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if "%PY%"=="" (
  where python >nul 2>nul && set "PY=python"
)
if "%PY%"=="" (
  where python3 >nul 2>nul && set "PY=python3"
)
if "%PY%"=="" (
  echo ERROR: Python 3 not found on PATH.
  echo Install it from https://www.python.org/downloads/ and try again.
  echo.
  pause
  exit /b 1
)

echo ==^> Rebuilding Nuclear Renaissance Index with today's prices
%PY% --version
echo.

%PY% build_static_site.py
if errorlevel 1 (
  echo.
  echo !! Build failed. Scroll up to see the error.
  echo    Tip: if network is unavailable, rebuild with a synthetic baseline:
  echo         %PY% build_static_site.py --offline
  echo.
  pause
  exit /b 1
)

echo.
echo ==^> Done. Rebuilt files:
if exist "%~dp0nri.html"                          echo     - %~dp0nri.html
if exist "%~dp0Nuclear-Renaissance-Index.html"    echo     - %~dp0Nuclear-Renaissance-Index.html
echo.
echo Open either file in your browser. The "Baked" badge should show "just now".
echo.
pause
