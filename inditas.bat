@echo off
setlocal EnableExtensions
title Torrent letolto

rem ---------------------------------------------------------------------------
rem  Torrent letolto - inditas Windows alatt.
rem
rem  Ez a fajl szandekosan nagyon egyszeru: megkeresi a Pythont, es atadja a
rem  vezerlest az indit.py-nak. Minden fuggoseg-ellenorzes ott van, mert azt
rem  lehet tesztelni - a cmd.exe-t nem.
rem
rem  Ezert nincs benne tobbsoros zarojeles blokk sem: ha a fajl valahogy megis
rem  LF sorveggel keruline a gepre, a cmd akkor sem tud rajta elcsuszni.
rem
rem  Ekezetet sem tartalmaz: a cmd a rendszer kodlapjaval olvas.
rem
rem  A Pythont mindig a "py -3" inditoval keressuk eloszor. A "python" nevet
rem  Windows 10/11 alatt egy Microsoft Store-hivatkozas is elfoglalhatja; a
rem  -c kapcsoloval indulva az csak hibat ad, a Store-t nem nyitja meg.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

echo ============================================================
echo   Torrent letolto   [indito v1]
echo ============================================================
echo.

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if defined PY goto :python_megvan
python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto :python_megvan
python3 -c "import sys" >nul 2>&1 && set "PY=python3"
if defined PY goto :python_megvan
goto :nincs_python

:python_megvan
if not exist "indit.py" goto :nincs_indito
%PY% indit.py
if errorlevel 1 goto :hiba
endlocal
exit /b 0

:nincs_python
echo [HIBA] Nem talalhato Python a rendszeren.
echo.
echo  Telepitsd innen: https://www.python.org/downloads/
echo  A telepitonel pipald be az "Add python.exe to PATH" opciot,
echo  valamint a "tcl/tk and IDLE" komponenst.
echo.
echo  Ha szerinted MAR telepitve van, akkor valoszinuleg a Windows sajat
echo  "python.exe" hivatkozasa all utban. Kapcsold ki itt:
echo    Gepbeallitasok - Alkalmazasok - Specialis alkalmazasbeallitasok
echo    - Alkalmazasvegrehajtasi alnevek - python.exe / python3.exe = Ki
goto :hiba

:nincs_indito
echo [HIBA] Nem talalom az indit.py fajlt ebben a mappaban:
echo        %CD%
echo  Ugy tunik, hianyos a kicsomagolt mappa.
goto :hiba

:hiba
echo.
pause
endlocal
exit /b 1
