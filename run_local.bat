@echo off
rem Cheape Deal - one-click setup and start (Windows). Double-click this file, or run it from cmd.
cd /d "%~dp0"
title Cheape Deal

set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD where python >nul 2>&1 && set "PYCMD=python"
if not defined PYCMD (
  echo.
  echo Python is not installed or not on PATH.
  echo Install Python 3.12 from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

%PYCMD% "%~dp0scripts\local_run.py" %*
echo.
pause
