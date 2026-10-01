@echo off
cd /d "%~dp0"
python check.py || py check.py
pause
