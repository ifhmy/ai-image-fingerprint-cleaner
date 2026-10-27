@echo off
title AI Image Fingerprint Cleaner - Web UI
cd /d "%~dp0"
if not exist "input" mkdir "input"
if not exist "output" mkdir "output"
python app.py
pause
