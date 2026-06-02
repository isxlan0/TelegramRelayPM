@echo off
setlocal

set "OUTDIR=dist"
set "APP=relaybot"

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo Building Linux amd64...
set GOOS=linux
set GOARCH=amd64
set CGO_ENABLED=0
go build -trimpath -ldflags "-s -w" -o "%OUTDIR%\%APP%_linux_amd64" .\cmd\relaybot
if errorlevel 1 goto failed

echo Building Windows amd64...
set GOOS=windows
set GOARCH=amd64
set CGO_ENABLED=0
go build -trimpath -ldflags "-s -w" -o "%OUTDIR%\%APP%_windows_amd64.exe" .\cmd\relaybot
if errorlevel 1 goto failed

echo.
echo Build completed:
echo   %OUTDIR%\%APP%_linux_amd64
echo   %OUTDIR%\%APP%_windows_amd64.exe
exit /b 0

:failed
echo Build failed.
exit /b 1
