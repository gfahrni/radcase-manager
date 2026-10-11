@echo off
setlocal
REM ===========================================================================
REM  Build du binaire portable Windows de RadCase Manager.
REM
REM  Prerequis : Python 3.10+ (64 bits) installe et accessible dans le PATH.
REM  Resultat  : dist\RadCaseManager.exe  (executable autonome, rien a installer
REM              sur le poste cible).
REM
REM  Usage : double-cliquer sur ce fichier, ou dans un terminal :
REM              build_windows.bat
REM ===========================================================================

cd /d "%~dp0"

echo [1/3] Preparation de l'environnement virtuel .venv ...
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo ECHEC : impossible de creer l'environnement virtuel.
        echo Verifiez que Python 3.10+ est installe et dans le PATH.
        pause
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

echo [2/3] Installation des dependances ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.0"
if errorlevel 1 (
    echo ECHEC : installation des dependances.
    pause
    exit /b 1
)

echo [3/3] Construction de l'executable ...
python -m PyInstaller --clean --noconfirm RadCaseManager.spec
if errorlevel 1 (
    echo ECHEC : construction de l'executable.
    pause
    exit /b 1
)

echo.
echo ===========================================================
echo  Termine !  Executable :  dist\RadCaseManager.exe
echo  Ce fichier unique est portable : le copier ou bon vous
echo  semble. Les reglages (settings_data) se creent a cote.
echo ===========================================================
pause
