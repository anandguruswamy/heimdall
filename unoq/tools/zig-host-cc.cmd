@echo off
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0zig-host-link.ps1" %*
exit /b %ERRORLEVEL%
