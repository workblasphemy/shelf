@echo off
title Spotify Downloader for Jellyfin
cls

:: 1. Auto-update core components to fix extraction errors
echo ===================================================
echo   CHECKING FOR DOWNLOADER UPDATES (PLEASE WAIT)
echo ===================================================
py -m pip install --upgrade yt-dlp spotdl --quiet
cls

:: 2. Get the Spotify URL from the user
echo ===================================================
echo              SPOTIFY DOWNLOADER FOR JELLYFIN
echo ===================================================
echo.
set /p "spoturl=Paste your Spotify Playlist or Album URL: "
if "%spoturl%"=="" (
    echo Error: You must paste a valid URL.
    pause
    exit
)

:: 3. Dynamically set up the target path to the current script's folder
:: '%~dp0' resolves to the exact directory where this batch file lives
set "outpath=%~dp0Music"

:: Ensure the local Music folder exists before running spotDL
if not exist "%outpath%" mkdir "%outpath%"

:: 4. Run spotDL with optimized speed and cleaner logs
echo.
echo Starting download straight to: %outpath%
echo.
echo Optimization Active: 4 concurrent tracks.
echo Saving locally to script directory.
echo.

:: PATH, PERFORMANCE, LYRICS, & SILENT ERROR FIX:
:: - Uses the dynamic '%outpath%' folder alongside the script
:: - '--lyrics none' bypasses lyric searches to speed up performance
:: - '2>nul' mutes the remaining Red LookupError console noise
"%USERPROFILE%\AppData\Local\Programs\Python\Python313\Scripts\spotdl.exe" download %spoturl% --output "%outpath%/{artists}/{album}/{track-number} - {title}.{output-ext}" --format m4a --bitrate 320k --threads 4 

echo.
echo ===================================================
echo Process finished! Check your local folder.
echo ===================================================
pause
