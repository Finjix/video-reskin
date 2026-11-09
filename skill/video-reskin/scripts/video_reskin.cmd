@echo off
setlocal
if defined VIDEO_RESKIN_CACHE_DIR (set "VR_CACHE=%VIDEO_RESKIN_CACHE_DIR%") else (set "VR_CACHE=%~dp0..\runtime-cache")
set "VIDEO_RESKIN_CACHE_DIR=%VR_CACHE%"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0video_reskin.ps1" __bootstrap
if errorlevel 1 exit /b %errorlevel%
"%VR_CACHE%\environment\Scripts\python.exe" -B "%~dp0video_reskin.py" %*
exit /b %errorlevel%
