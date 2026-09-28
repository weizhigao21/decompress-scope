@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem JieyaKaijing Windows build script (PyInstaller onefile)
rem Output: dist\JieyaKaijing.exe
rem ============================================================
title JieyaKaijing - Build
set "APP_NAME=JieyaKaijing"

echo ============================================
echo   JieyaKaijing Windows build
echo ============================================
echo.

rem Find Python 3.10+. Keep this file ASCII-only: CMD parses a complete
rem parenthesized block before executing it and is not UTF-8-safe there.
set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.10 -c "pass" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3.10"
)
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys; assert sys.version_info[0] == 3 and sys.version_info[1] in [10,11,12,13,14,15,16,17,18,19,20]" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -3"
    )
)
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; assert sys.version_info[0] == 3 and sys.version_info[1] in [10,11,12,13,14,15,16,17,18,19,20]" >nul 2>nul
        if not errorlevel 1 set "PYCMD=python"
    )
)
if not defined PYCMD goto :no_python

echo [1/5] Python: %PYCMD%
%PYCMD% --version

%PYCMD% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [2/5] Installing PySide6 ...
    %PYCMD% -m pip install "PySide6>=6.5"
    if errorlevel 1 goto :fail
) else (
    echo [2/5] PySide6 is ready.
)

%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [3/5] Installing PyInstaller ...
    %PYCMD% -m pip install "pyinstaller>=6"
    if errorlevel 1 goto :fail
) else (
    echo [3/5] PyInstaller is ready.
)

echo [4/5] Building. This may take a few minutes ...
if exist "dist\%APP_NAME%\" move "dist\%APP_NAME%" "dist\old_%RANDOM%%RANDOM%" >nul
if exist "dist\%APP_NAME%.exe" move "dist\%APP_NAME%.exe" "dist\old_%RANDOM%%RANDOM%.exe" >nul
set "PYINSTALLER_CONFIG_DIR=%CD%\build\pyinstaller-cache"

rem UPX can corrupt or make Qt extension modules unloadable on some PCs.
%PYCMD% -m PyInstaller --noconfirm --clean --noupx --onefile --windowed ^
  --name "%APP_NAME%" ^
  --icon "ui\assets\app.ico" ^
  --add-data "ui\assets;ui\assets" ^
  --hidden-import ui.main_window ^
  --hidden-import ui.quick_extract_window ^
  --hidden-import core.shell_menu ^
  main.py
if errorlevel 1 goto :fail

set "OUT=dist\%APP_NAME%.exe"
if not exist "%OUT%" goto :missing_output
for /d %%d in ("dist\old_*") do rmdir /s /q "%%d" 2>nul
for %%f in ("dist\old_*.exe") do del /q "%%f" 2>nul
rem PyInstaller puts a non-distributable intermediate EXE in build\%APP_NAME%.
rem Remove it so users can only pick the tested file in dist.
if exist "build\%APP_NAME%" rmdir /s /q "build\%APP_NAME%" 2>nul

echo [5/5] Build succeeded: %OUT%
echo.
echo Runtime settings and data are stored in Local AppData. See README.md.
echo 7-Zip is not bundled. Install 7-Zip or set SEVENZIP_PATH on the target PC.
echo Run the exe once, then enable the Explorer menu in Settings if needed.
echo.
if not defined JYK_SKIP_OPEN explorer /select,"%CD%\%OUT%" >nul 2>nul
pause
exit /b 0

:no_python
echo [ERROR] Python 3.10 or newer was not found.
echo Install Python and enable Add Python to PATH, then run this script again.
goto :fail

:missing_output
echo [ERROR] Build finished but expected output is missing: %OUT%
goto :fail

:fail
echo.
echo [ERROR] Build failed. A previous build, if any, remains in dist\old_*.
pause
exit /b 1
