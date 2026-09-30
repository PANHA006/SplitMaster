@echo off
title Splitify - Video Splitter
cd /d "%~dp0"

echo ============================================================
echo           Splitify - Professional Video Splitter
echo ============================================================
echo.
echo Starting Splitify application...
echo Server running on http://127.0.0.1:8765
echo.

python main.py

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to run Splitify.
    echo.
    pause
)
