@echo off
setlocal
cd /d "%~dp0"

set "MUSIC=%USERPROFILE%\Music"
set "PASS=change-this-password"

where py >nul 2>&1 && (
  echo.
  echo On this PC:  http://127.0.0.1:8787
  echo On the phone, use the Wi-Fi IPv4 from ipconfig, not a vEthernet or Tailscale address.
  echo If the phone says it cannot connect, run open-firewall.bat as administrator.
  echo.
  py -3 shelf.py --music "%MUSIC%" --password "%PASS%"
  goto :done
)
where python >nul 2>&1 && (
  python shelf.py --music "%MUSIC%" --password "%PASS%"
  goto :done
)

echo Python is not installed. The "python3" command on this PC is a Microsoft Store stub.
echo.
echo 1. Download Python from https://www.python.org/downloads/windows/
echo 2. Run the installer and check "Add python.exe to PATH"
echo 3. Close this window, open a new one, and run start-shelf.bat again
echo.
start https://www.python.org/downloads/windows/
pause
exit /b 1

:done
echo.
echo Shelf stopped.
pause
