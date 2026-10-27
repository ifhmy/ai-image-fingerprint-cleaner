@echo off
title Quick Batch Clean Folder
cd /d "%~dp0"
if not exist "input" mkdir "input"
if not exist "output" mkdir "output"
echo ============================================================
echo   جاري معالجة وتنظيف المجلدات والصور تلقائيا...
echo ============================================================
python cleaner_engine.py --input "input" --output "output" --crop 0 --format JPEG --quality 96 --folder-mode subfolder_clean --folder-suffix _clean
echo.
echo ============================================================
echo   اكتمل التنظيف بنجاح! جاري فتح مجلد الصور (output)...
echo ============================================================
explorer "output"
pause
