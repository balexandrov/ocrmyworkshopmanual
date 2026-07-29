@echo off
rem ============================================================================
rem  Target of the Explorer "Compress + OCR" context menu on a .pdf file
rem  (see compress-pdf-context-menu.reg beside this file).
rem
rem  Usage:  compress_pdf_here.cmd "<file.pdf>" [extra ocrmyworkshopmanual.py flags...]
rem
rem  Default options mean the ORIGINAL IS NOT TOUCHED: the result is written as a
rem  sibling "<name> (COMPRESSED).pdf". Pass --in-place from a menu entry if you
rem  ever want the source overwritten instead.
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
    echo ERROR: no file given.
    echo        Right-click a .pdf and pick "Compress + OCR", or run:
    echo          "%~nx0" "C:\some\manual.pdf"
    goto :done
)
if not exist "%~1" (
    echo ERROR: no such file: %1
    goto :done
)
if /i not "%~x1"==".pdf" (
    echo ERROR: not a PDF: %1
    echo        This tool is for scanned PDFs only.
    goto :done
)

echo File  : %1
echo Output: "%~dpn1 (COMPRESSED)%~x1"
echo Python: "%PY%"
echo.
echo Compressing + adding a searchable text layer. A big scan can take a while;
echo the original is left untouched either way. Read the result below before
echo closing this window -- the menu passes --no-log, so nothing is written to
echo disk except the (COMPRESSED) file itself.
echo.

"%PY%" "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo ---- FAILED with exit code %RC%. Your original file is unchanged.
) else (
    echo ---- Done.
)

:done
echo.
pause
endlocal
