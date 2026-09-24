@echo off
setlocal
cd /d "%~dp0"

echo [StoryFlow] Checking bootstrap state...
python bootstrap.py --check
if errorlevel 1 (
    echo [StoryFlow] Bootstrap incomplete. Running bootstrap...
    python bootstrap.py
    if errorlevel 1 (
        echo [StoryFlow] Bootstrap FAILED. Fix the error above and retry.
        exit /b 1
    )
)
echo [StoryFlow] Bootstrap OK. Application startup is not implemented yet (Phase 0).
exit /b 0