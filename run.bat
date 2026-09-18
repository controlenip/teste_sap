@echo off
cd /d "%~dp0"
set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  echo Python nao foi encontrado no PATH.
  pause
  exit /b 1
)
%PYTHON_CMD% -m streamlit run app.py
pause
