@echo off
cd /d "%~dp0"

set DIST=suyog-dist
set ZIP=suyog-dist.zip

echo Cleaning previous build...
if exist "%DIST%" rmdir /s /q "%DIST%"
if exist "%ZIP%" del "%ZIP%"

echo Creating distribution folder...
mkdir "%DIST%"

echo Copying source files...
copy /y launch.bat "%DIST%\"
copy /y run.bat "%DIST%\"
copy /y menu.bat "%DIST%\"
copy /y pack.bat "%DIST%\"
copy /y pyproject.toml "%DIST%\"
copy /y service_requirements.txt "%DIST%\"
copy /y urls.txt "%DIST%\"
copy /y config.yaml "%DIST%\"
copy /y case.py "%DIST%\"
copy /y capture_png.py "%DIST%\"
copy /y seo_capture.py "%DIST%\"
copy /y png_to_pdf.py "%DIST%\"
copy /y page_capture.py "%DIST%\"

echo Creating zip...
powershell -Command "Compress-Archive -Path '%DIST%\*' -DestinationPath '%ZIP%' -Force"

echo Cleaning up...
rmdir /s /q "%DIST%"

echo Done: %ZIP%