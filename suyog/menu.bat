@echo off
title SnapShot - Batch Capture

:MENU
cls
echo ========================================
echo        SnapShot - Batch Capture
echo ========================================
echo.
echo [1] Capture PNG + CSV   (case.py)
echo [2] Capture PNG Only    (capture_png.py)
echo [3] Extract SEO Data    (seo_capture.py)
echo [4] Convert PNG to PDF  (png_to_pdf.py)
echo [5] Exit
echo.

set /p CHOICE=Select mode [1-5]: 

if "%CHOICE%"=="1" goto CASE
if "%CHOICE%"=="2" goto PNG_ONLY
if "%CHOICE%"=="3" goto SEO
if "%CHOICE%"=="4" goto PNG2PDF
if "%CHOICE%"=="5" goto EXIT

echo Invalid choice. Press any key to retry...
pause >nul
goto MENU

:SELECT_URLS
echo.
echo URL file options:
echo [1] urls.txt (default)
echo [2] Custom file
echo.
set /p URL_CHOICE=Select [1-2]: 

if "%URL_CHOICE%"=="2" (
    set /p CUSTOM_URL_FILE=Enter path to URL file: 
    if not exist "%CUSTOM_URL_FILE%" (
        echo File not found: %CUSTOM_URL_FILE%
        pause
        goto MENU
    )
    set URL_FILE=%CUSTOM_URL_FILE%
) else (
    set URL_FILE=urls.txt
)
goto :EOF

:CASE
call :SELECT_URLS
uv run python case.py
pause
goto MENU

:PNG_ONLY
call :SELECT_URLS
uv run python capture_png.py
pause
goto MENU

:SEO
call :SELECT_URLS
uv run python seo_capture.py
pause
goto MENU

:PNG2PDF
uv run python png_to_pdf.py
pause
goto MENU

:EXIT
echo Goodbye!
timeout /t 1 >nul