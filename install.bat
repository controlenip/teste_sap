@echo off
cd /d "%~dp0"
set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py"
if not defined PYTHON_CMD where python >nul 2>&1 && set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  echo Python nao foi encontrado no PATH.
  echo Instale Python 3.11 ou 3.12 no seu computador local e tente novamente.
  pause
  exit /b 1
)

echo ============================================
echo  Instalando dependencias do Robo SAP
echo ============================================
%PYTHON_CMD% -m pip install --upgrade pip
%PYTHON_CMD% -m pip install -r requirements.txt

echo.
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
  echo Tesseract detectado em C:\Program Files\Tesseract-OCR\tesseract.exe
) else (
  where tesseract >nul 2>&1
  if errorlevel 1 (
    echo ATENCAO: Tesseract OCR nao foi detectado.
    echo Instale o Tesseract OCR no seu Windows local.
    echo Caminho comum: C:\Program Files\Tesseract-OCR\tesseract.exe
  ) else (
    echo Tesseract detectado no PATH.
  )
)

echo.
echo Instalacao das dependencias Python finalizada.
pause
