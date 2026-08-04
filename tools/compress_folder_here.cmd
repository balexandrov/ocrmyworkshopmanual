@echo off
rem ============================================================================
rem  Target of the Explorer "Compress + OCR this folder (in place)" context menu
rem  on a FOLDER (see compress-folder-context-menu.reg beside this file).
rem
rem  Usage:  compress_folder_here.cmd "<folder>" [extra ocrmyworkshopmanual.py flags...]
rem
rem  The menu passes --in-place, which OVERWRITES each scanned PDF in the tree
rem  with its compressed + OCR'd result. Subfolders are included: pointing the
rem  tool at a folder always walks the whole tree, there is no separate flag.
rem  Because that is destructive and one stray right-click away, this file asks
rem  for a typed confirmation before it starts. Everything else is defaults --
rem  no --log, so nothing but the PDFs themselves is written to disk, and the
rem  run reports to this console only.
rem
rem  The registry holds only the path to THIS file, so finding the interpreter,
rem  quoting a name with spaces and keeping the window open live here where they
rem  can be read and fixed, not inside an escaped registry string.
rem ============================================================================
setlocal EnableExtensions

rem  Repo root is this file's folder minus \tools, so the checkout can be moved
rem  or renamed without editing anything but the one path in the .reg.
for %%I in ("%~dp0..") do set "REPO=%%~fI"
set "SCRIPT=%REPO%\ocrmyworkshopmanual.py"

if not exist "%SCRIPT%" (
    echo ERROR: cannot find "%SCRIPT%".
    echo        This .cmd must stay in the repo's tools\ folder.
    goto :done
)

rem  Prefer the repo's own virtualenv: a bare system python will not have pypdf,
rem  pikepdf, img2pdf, numpy, scipy or Pillow.
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"

if "%~1"=="" (
    echo ERROR: no folder given.
    echo        Right-click a folder and pick "Compress + OCR this folder", or run:
    echo          "%~nx0" "C:\some\manuals"
    goto :done
)
if not exist "%~1\" (
    echo ERROR: not a folder: %1
    echo        This entry is for folders. For one PDF, right-click the file instead.
    goto :done
)

echo Folder: %1
echo Output: IN PLACE -- every scanned PDF in this folder AND its subfolders is
echo         OVERWRITTEN with its compressed, searchable version.
echo Python: "%PY%"
echo.
echo A born-digital PDF is never rasterised, files that are already optimal are
echo left untouched, and a file is only replaced once its result verifies -- but
echo this still rewrites originals. Make sure you have a backup.
echo.

set "OK="
set /p "OK=Type YES to proceed (anything else cancels): "
if /i not "%OK%"=="YES" (
    echo.
    echo Cancelled. Nothing was changed.
    goto :done
)

echo.
echo Working. A large tree can take hours; read the summary below before closing
echo this window -- no report file is written unless you add --log.
echo.

"%PY%" "%SCRIPT%" %* --in-place
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo ---- FAILED with exit code %RC%. Files that were not reached are unchanged.
) else (
    echo ---- Done.
)

:done
echo.
pause
endlocal
