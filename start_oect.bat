@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
    echo Please complete the installation steps in README.md first.
    echo The required environment is .venv in this folder.
    popd
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m OECT.cli %*
set "OECT_EXIT_CODE=%ERRORLEVEL%"
popd
if not "%OECT_EXIT_CODE%"=="0" (
    echo OECT exited with error code %OECT_EXIT_CODE%.
    pause
)
exit /b %OECT_EXIT_CODE%
