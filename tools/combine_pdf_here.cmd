@echo off
rem ============================================================================
rem  Target of the Explorer "Combine PDF" context menu (see the .reg files here).
rem
rem  Usage:  combine_pdf_here.cmd "<folder>" [extra combine_manual.py flags...]
rem
rem  The registry holds only the path to THIS file, so the fiddly parts -- finding
rem  the interpreter, quoting a folder name with spaces, keeping the window open
rem  so you can read the page order and any error -- live here where they can be
rem  read and fixed, not inside an escaped registry string.
rem ============================================================================
setlocal EnableExtensions

rem  Repo root is this file's folder minus \tools, so the checkout can be moved
rem  or renamed without editing anything but the one path in the .reg.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
set "SCRIPT=%REPO%\combine_manual.py"

if not exist "%SCRIPT%" (
    echo ERROR: cannot find "%SCRIPT%".
    echo        This .cmd must stay in the repo's tools\ folder.
    goto :done
)

rem  Prefer the repo's own virtualenv: the script needs pypdf, img2pdf and Pillow,
rem  and a bare system python almost certainly does not have them.
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"

if "%~1"=="" (
    echo ERROR: no folder given.
    echo        Right-click a FOLDER and pick "Combine PDF", or run:
    echo          "%~nx0" "C:\some\folder" [--dry-run] [--recursive]
    goto :done
)
if not exist "%~1\" (
    echo ERROR: not a folder: %1
    goto :done
)

echo Folder: %1
echo Script: "%SCRIPT%"
echo Python: "%PY%"
echo.

rem  %* forwards the quoted folder plus whatever flags the menu entry added.
"%PY%" "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo ---- FAILED with exit code %RC%. Nothing was deleted; the folder is untouched.
) else (
    echo ---- Done.
)

:done
echo.
pause
endlocal
