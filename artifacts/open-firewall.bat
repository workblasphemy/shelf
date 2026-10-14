@echo off
netsh advfirewall firewall add rule name="Shelf music" dir=in action=allow protocol=TCP localport=8787 profile=private
if errorlevel 1 (
  echo Run this file as administrator: right-click, Run as administrator.
  pause
  exit /b 1
)
echo Port 8787 is allowed on private networks.
pause
