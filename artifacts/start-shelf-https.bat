@echo off
setlocal
cd /d "%~dp0"
set "MUSIC=%USERPROFILE%\Music"
set "PASS=change-this-password"

where py >nul 2>&1 && (
  echo On the phone open https://10.0.0.197:8787
  echo Accept the certificate warning, then log in.
  py -3 shelf.py --music "%MUSIC%" --password "%PASS%" --https
  goto :done
)
echo Python was not found.
pause
exit /b 1

:done
pause
