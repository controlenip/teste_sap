@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo  BUILD - SAP Fotos (Windows)
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERRO] Python nao encontrado nesta maquina de BUILD.
  echo Use o workflow do GitHub Actions se nao quiser instalar Python localmente.
  pause
  exit /b 1
)

python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 exit /b 1

if not exist "vendor\tesseract\tesseract.exe" (
  echo.
  echo [AVISO] vendor\tesseract\tesseract.exe nao encontrado.
  echo Para EXE sem instalacao no PC de destino, copie uma distribuicao Tesseract Windows
  echo completa para vendor\tesseract ou use o workflow GitHub, que faz isso automaticamente.
  pause
  exit /b 2
)

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

python -m PyInstaller --noconfirm --clean SAP_Fotos_onefile.spec
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean SAP_Fotos_portable.spec
if errorlevel 1 exit /b 1

xcopy /E /I /Y "vendor\tesseract" "dist\SAP_Fotos_Portatil\tesseract" >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; if(Test-Path 'dist\SAP_Fotos_Portatil.zip'){Remove-Item 'dist\SAP_Fotos_Portatil.zip' -Force}; Compress-Archive -Path 'dist\SAP_Fotos_Portatil\*' -DestinationPath 'dist\SAP_Fotos_Portatil.zip' -CompressionLevel Optimal; Get-FileHash 'dist\SAP_Fotos.exe' -Algorithm SHA256 ^| Format-List ^| Out-File 'dist\SHA256.txt'; Get-FileHash 'dist\SAP_Fotos_Portatil.zip' -Algorithm SHA256 ^| Format-List ^| Out-File 'dist\SHA256.txt' -Append"

echo.
echo Build concluido.
echo   dist\SAP_Fotos.exe
 echo  dist\SAP_Fotos_Portatil.zip
 echo  dist\SHA256.txt
pause
