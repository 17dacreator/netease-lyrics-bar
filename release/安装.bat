@echo off
rem Puts a desktop shortcut in place and opens the lyric bar. Safe to run again after updating.
cd /d "%~dp0"
taskkill /im NeteaseLyrics.exe /f >nul 2>&1
start /wait "" NeteaseLyrics.exe --install
start "" NeteaseLyrics.exe
echo.
echo   OK - shortcut added to your desktop, the lyric bar is starting.
echo.
timeout /t 4 >nul
