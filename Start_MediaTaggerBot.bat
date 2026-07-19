@echo off
rem MediaTaggerBot v0.5.4 Windows launcher.
setlocal EnableExtensions DisableDelayedExpansion

title MediaTaggerBot v0.5.4 BAT Menu
set "PROJECT_ROOT=%~dp0"
if "%PROJECT_ROOT:~-1%"=="\" set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"
cd /d "%PROJECT_ROOT%" || (
    echo Failed to enter project folder: %PROJECT_ROOT%
    pause
    exit /b 1
)

for %%D in ("logs" "logs\batch_runs" "exports" "diagnostics" "state" "temp" "config\backups") do (
    if not exist "%%~D" mkdir "%%~D" >nul 2>nul
)

:menu
cls
call :header
echo.
echo Choose a mode:
echo   1. Preflight        - validate config, target, tools, and API-key presence
echo   2. Scan-only        - recursively inventory root + all reachable subfolders
echo   3. Dry-run          - match and propose names; no media changes
echo   4. Apply-safe       - tag/rename high-confidence public-DB matches
echo   5. Apply-all        - aggressive autopilot; lower-confidence matches allowed
echo   6. Diagnostics      - always create a compact ZIP, even if config is malformed
echo   7. Rollback         - restore filenames from rollback_manifest_*.json
echo   8. Set media root   - safely save/change the target folder
echo   9. Repair/check     - path, config-recovery, and portability check
echo  10. Edit config      - Notepad edit with backup + post-save validation
echo  11. Open exports     - open reports folder
echo  12. Open diagnostics - open diagnostics folder
echo  13. Open batch logs  - open full batch-output transcripts
echo  14. Request stop     - gracefully finalize the active long run
echo   0. Exit
echo.
set "CHOICE="
set /p "CHOICE=Selection: "

if "%CHOICE%"=="1" goto menu_preflight
if "%CHOICE%"=="2" goto menu_scan
if "%CHOICE%"=="3" goto menu_dry
if "%CHOICE%"=="4" goto menu_safe
if "%CHOICE%"=="5" goto menu_all
if "%CHOICE%"=="6" goto menu_diagnostics
if "%CHOICE%"=="7" goto menu_rollback
if "%CHOICE%"=="8" goto menu_setroot
if "%CHOICE%"=="9" goto menu_repair
if "%CHOICE%"=="10" goto menu_edit
if "%CHOICE%"=="11" goto menu_exports
if "%CHOICE%"=="12" goto menu_diagnostics_folder
if "%CHOICE%"=="13" goto menu_logs
if "%CHOICE%"=="14" goto menu_stop
if "%CHOICE%"=="0" exit /b 0

echo Invalid selection: %CHOICE%
pause
goto menu

:menu_preflight
call :runmode preflight
goto menu

:menu_scan
call :runmode scan-only
goto menu

:menu_dry
call :runmode dry-run
goto menu

:menu_safe
call :runmode apply-safe
goto menu

:menu_all
call :runmode apply-all
goto menu

:menu_diagnostics
call :runmode diagnostics
goto menu

:menu_rollback
call :runmode rollback
goto menu

:menu_setroot
call :runmode set-root
goto menu

:menu_repair
call :runmode repair
goto menu

:menu_edit
call :editconfig
goto menu

:menu_exports
call :openfolder "exports"
goto menu

:menu_diagnostics_folder
call :openfolder "diagnostics"
goto menu

:menu_logs
call :openfolder "logs\batch_runs"
goto menu

:menu_stop
call :runmode request-stop
goto menu

:header
echo ===============================================
echo  MediaTaggerBot v0.5.4
echo  Triage-aware + graceful-stop + offline pinned runtime
echo ===============================================
echo Project: %PROJECT_ROOT%
if exist "%PROJECT_ROOT%\Launch_MediaTaggerBot.ps1" echo Note: legacy PowerShell launcher detected; it is ignored by this BAT.
exit /b 0

:runmode
set "MODE=%~1"
set "CONFIG_BACKUP_ARG=%~2"
set "ROOT_ARG="
set "ROLLBACK_ARG="

if /I "%MODE%"=="scan-only" call :askroot_optional
if /I "%MODE%"=="dry-run" call :askroot_optional
if /I "%MODE%"=="apply-safe" call :askroot_optional
if /I "%MODE%"=="apply-all" call :askroot_optional
if /I "%MODE%"=="set-root" call :askroot_required
if /I "%MODE%"=="set-root" if errorlevel 1 exit /b 0

if /I "%MODE%"=="apply-safe" call :confirmapply "%MODE%"
if /I "%MODE%"=="apply-safe" if errorlevel 1 exit /b 0
if /I "%MODE%"=="apply-all" call :confirmapply "%MODE%"
if /I "%MODE%"=="apply-all" if errorlevel 1 exit /b 0

if /I "%MODE%"=="rollback" call :askrollback
if /I "%MODE%"=="rollback" if errorlevel 1 exit /b 0
if /I "%MODE%"=="rollback" call :confirmapply "%MODE%"
if /I "%MODE%"=="rollback" if errorlevel 1 exit /b 0

call :timestamp
set "LOG_FILE=%PROJECT_ROOT%\logs\batch_runs\%STAMP%_%MODE%.txt"
set "LEGACY_PS1=no"
if exist "%PROJECT_ROOT%\Launch_MediaTaggerBot.ps1" set "LEGACY_PS1=yes_ignored"

> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo MediaTaggerBot BAT run transcript
>> "%LOG_FILE%" echo Version: v0.5.4
>> "%LOG_FILE%" echo Started: %DATE% %TIME%
>> "%LOG_FILE%" echo ProjectRoot: %PROJECT_ROOT%
>> "%LOG_FILE%" echo Mode: %MODE%
if defined ROOT_ARG (>> "%LOG_FILE%" echo RootOverrideProvided: yes) else (>> "%LOG_FILE%" echo RootOverrideProvided: no)
if defined ROLLBACK_ARG (>> "%LOG_FILE%" echo RollbackManifestProvided: yes) else (>> "%LOG_FILE%" echo RollbackManifestProvided: no)
>> "%LOG_FILE%" echo Launcher: BAT direct to local Python; path values transferred by environment, not argv quoting
>> "%LOG_FILE%" echo ConfigSafety: parse fallback for diagnostics; narrow backed-up Windows-path repair; atomic validated writes
>> "%LOG_FILE%" echo RecursiveCoverage: strict proof written by scanner; no named exclusions by default
>> "%LOG_FILE%" echo ApplySafety: source-change guard; metadata readback; rename verification; durable journal
>> "%LOG_FILE%" echo IdentitySafety: stable IDs first; version-aware candidate margin; ambiguity blocked in apply-safe
>> "%LOG_FILE%" echo TriageExit: fail-closed Critical gates; graceful stop; truthful run_exit_report JSON
>> "%LOG_FILE%" echo StopSafety: request-stop bypasses runtime setup and never rebuilds the active .venv
>> "%LOG_FILE%" echo DependencySafety: exact-version hash-checked lock; package index used only when dependencies are absent
>> "%LOG_FILE%" echo LegacyPowerShellPresent: %LEGACY_PS1%
>> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo.

echo.
echo Running MediaTaggerBot mode: %MODE%
echo Full batch output will be saved to:
echo   %LOG_FILE%
echo.

call :execute_mode >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ==================================================
>> "%LOG_FILE%" echo Finished: %DATE% %TIME%
>> "%LOG_FILE%" echo ExitCode: %EXIT_CODE%
>> "%LOG_FILE%" echo ==================================================

echo.
echo ---------------- FULL BATCH OUTPUT ----------------
type "%LOG_FILE%"
echo ---------------------------------------------------
echo.
echo Transcript saved:
echo   %LOG_FILE%
echo.
if "%EXIT_CODE%"=="75" echo Graceful stop completed: partial reports, diagnostics, and run-exit evidence were finalized.
if not "%EXIT_CODE%"=="0" if not "%EXIT_CODE%"=="75" echo Run finished with exit code %EXIT_CODE%. The transcript and diagnostic ZIP contain the recovery details.
pause
exit /b 0

:execute_mode
echo [launcher] Preparing launcher handoff...
call :set_launcher_environment
if /I "%MODE%"=="request-stop" goto execute_request_stop_control
if /I "%MODE%"=="diagnostics" goto execute_python_control
if /I "%MODE%"=="repair" goto execute_python_control
if /I "%MODE%"=="set-root" goto execute_python_control
if /I "%MODE%"=="validate-config" goto execute_python_control
if /I "%MODE%"=="rollback" goto execute_python_control

echo [launcher] Preparing project-local Python runtime...
call :ensure_runtime
if errorlevel 1 (
    set "PY_EXIT=%ERRORLEVEL%"
    call :clear_launcher_environment
    exit /b %PY_EXIT%
)

set "PYTHONPATH=%PROJECT_ROOT%\src"
set "PATH=%PROJECT_ROOT%\tools;%PROJECT_ROOT%\tools\ffmpeg\bin;%PROJECT_ROOT%\tools\chromaprint;%PROJECT_ROOT%\tools\exiftool;%PATH%"
set "MEDIATAGGERBOT_ROOT_OVERRIDE="
set "MEDIATAGGERBOT_ROLLBACK_MANIFEST="
set "MEDIATAGGERBOT_CONFIG_BACKUP="
if defined ROOT_ARG set "MEDIATAGGERBOT_ROOT_OVERRIDE=%ROOT_ARG%"
if defined ROLLBACK_ARG set "MEDIATAGGERBOT_ROLLBACK_MANIFEST=%ROLLBACK_ARG%"
if defined CONFIG_BACKUP_ARG set "MEDIATAGGERBOT_CONFIG_BACKUP=%CONFIG_BACKUP_ARG%"

echo [launcher] Python: %VENV_PY%
echo [launcher] PYTHONPATH: %PYTHONPATH%
echo [launcher] BAT handshake: version/root/transcript are attested to Python.
echo [launcher] Root/manifest values use environment transport, so drive roots and trailing backslashes are safe.
echo [launcher] Portable tool folders added to PATH: tools; tools\ffmpeg\bin; tools\chromaprint; tools\exiftool
echo [launcher] Starting mode: %MODE%

"%VENV_PY%" -m mediataggerbot --mode "%MODE%"
set "PY_EXIT=%ERRORLEVEL%"
set "MEDIATAGGERBOT_ROOT_OVERRIDE="
set "MEDIATAGGERBOT_ROLLBACK_MANIFEST="
set "MEDIATAGGERBOT_CONFIG_BACKUP="
call :clear_launcher_environment
exit /b %PY_EXIT%

:execute_python_control
echo [launcher] This recovery/diagnostic mode uses a dependency-free control runtime.
echo [launcher] It does not create, delete, rebuild, or install into .venv.
call :find_control_python
if errorlevel 1 (
    set "PY_EXIT=%ERRORLEVEL%"
    call :clear_launcher_environment
    exit /b %PY_EXIT%
)
set "PYTHONPATH=%PROJECT_ROOT%\src"
set "PATH=%PROJECT_ROOT%\tools;%PROJECT_ROOT%\tools\ffmpeg\bin;%PROJECT_ROOT%\tools\chromaprint;%PROJECT_ROOT%\tools\exiftool;%PATH%"
set "MEDIATAGGERBOT_ROOT_OVERRIDE="
set "MEDIATAGGERBOT_ROLLBACK_MANIFEST="
set "MEDIATAGGERBOT_CONFIG_BACKUP="
if defined ROOT_ARG set "MEDIATAGGERBOT_ROOT_OVERRIDE=%ROOT_ARG%"
if defined ROLLBACK_ARG set "MEDIATAGGERBOT_ROLLBACK_MANIFEST=%ROLLBACK_ARG%"
if defined CONFIG_BACKUP_ARG set "MEDIATAGGERBOT_CONFIG_BACKUP=%CONFIG_BACKUP_ARG%"
echo [launcher] Control Python: %CONTROL_PY% %CONTROL_SWITCH%
echo [launcher] Runtime policy: no venv creation/rebuild or dependency install; diagnostics never bootstrap config.toml.
if defined CONTROL_SWITCH "%CONTROL_PY%" %CONTROL_SWITCH% -m mediataggerbot --mode "%MODE%"
if not defined CONTROL_SWITCH "%CONTROL_PY%" -m mediataggerbot --mode "%MODE%"
set "PY_EXIT=%ERRORLEVEL%"
set "MEDIATAGGERBOT_ROOT_OVERRIDE="
set "MEDIATAGGERBOT_ROLLBACK_MANIFEST="
set "MEDIATAGGERBOT_CONFIG_BACKUP="
call :clear_launcher_environment
exit /b %PY_EXIT%

:execute_request_stop_control
echo [launcher] Request-stop uses a standard-library control path.
echo [launcher] It will not create, delete, rebuild, or install into .venv.
set "PYTHONPATH=%PROJECT_ROOT%\src"
call :find_control_python
if errorlevel 1 (
    set "PY_EXIT=%ERRORLEVEL%"
    call :clear_launcher_environment
    exit /b %PY_EXIT%
)
echo [launcher] Control Python: %CONTROL_PY% %CONTROL_SWITCH%
if defined CONTROL_SWITCH "%CONTROL_PY%" %CONTROL_SWITCH% "%PROJECT_ROOT%\scripts\request_stop.py"
if not defined CONTROL_SWITCH "%CONTROL_PY%" "%PROJECT_ROOT%\scripts\request_stop.py"
set "PY_EXIT=%ERRORLEVEL%"
call :clear_launcher_environment
exit /b %PY_EXIT%

:set_launcher_environment
set "MEDIATAGGERBOT_LAUNCHER_KIND=bat_menu"
set "MEDIATAGGERBOT_LAUNCHER_VERSION=0.5.4"
set "MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT=%PROJECT_ROOT%"
set "MEDIATAGGERBOT_BATCH_LOG=%LOG_FILE%"
exit /b 0

:clear_launcher_environment
set "MEDIATAGGERBOT_LAUNCHER_KIND="
set "MEDIATAGGERBOT_LAUNCHER_VERSION="
set "MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT="
set "MEDIATAGGERBOT_BATCH_LOG="
exit /b 0

:find_control_python
set "CONTROL_PY="
set "CONTROL_SWITCH="
set "VENV_PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" "%VENV_PY%" -c "import platform,sys; ok=sys.version_info[:2] in {(3,11),(3,12),(3,13),(3,14)} and platform.machine().lower() in {'amd64','x86_64'}; raise SystemExit(0 if ok else 1)" >nul 2>nul
if exist "%VENV_PY%" if not errorlevel 1 set "CONTROL_PY=%VENV_PY%"
if defined CONTROL_PY exit /b 0
call :find_base_python
if errorlevel 1 exit /b %ERRORLEVEL%
set "CONTROL_PY=%PY_EXE%"
set "CONTROL_SWITCH=%PY_SWITCH%"
exit /b 0

:ensure_runtime
set "VENV_PY=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "DEPS_MARKER=%PROJECT_ROOT%\.venv\.deps_checked_v0.5.4"

if exist "%VENV_PY%" "%VENV_PY%" -c "import platform,sys; ok=sys.version_info[:2] in {(3,11),(3,12),(3,13),(3,14)} and platform.machine().lower() in {'amd64','x86_64'}; raise SystemExit(0 if ok else 1)" >nul 2>nul
if exist "%VENV_PY%" if not errorlevel 1 goto runtime_python_ready

if exist "%PROJECT_ROOT%\.venv" (
    echo [launcher] Existing .venv is missing or stale after a move; rebuilding reproducible dependencies.
    rmdir /s /q "%PROJECT_ROOT%\.venv"
    if exist "%PROJECT_ROOT%\.venv" (
        echo ERROR: Could not remove stale .venv. Close programs using it, then rerun.
        exit /b 12
    )
)

call :find_base_python
if errorlevel 1 exit /b %ERRORLEVEL%

echo [launcher] Creating local virtual environment...
if defined PY_SWITCH "%PY_EXE%" %PY_SWITCH% -m venv "%PROJECT_ROOT%\.venv"
if not defined PY_SWITCH "%PY_EXE%" -m venv "%PROJECT_ROOT%\.venv"
if errorlevel 1 (
    echo ERROR: Failed to create a supported 64-bit Python virtual environment.
    exit /b 13
)

:runtime_python_ready
if not exist "%VENV_PY%" (
    echo ERROR: Local Python runtime was not created: %VENV_PY%
    exit /b 14
)

"%VENV_PY%" -c "from importlib.metadata import version; expected={'requests':'2.32.5','mutagen':'1.47.0','charset-normalizer':'3.4.9','idna':'3.18','urllib3':'2.7.0','certifi':'2026.6.17'}; raise SystemExit(0 if all(version(k)==v for k,v in expected.items()) else 1)" >nul 2>nul
if not errorlevel 1 goto dependencies_ready

if not exist "%PROJECT_ROOT%\requirements.lock.txt" (
    echo ERROR: Missing requirements.lock.txt. Restore the file from the repository.
    exit /b 15
)

echo [launcher] Installing hash-checked dependencies from the configured Python package index...
"%VENV_PY%" -m pip install --disable-pip-version-check --require-hashes -r "%PROJECT_ROOT%\requirements.lock.txt"
if errorlevel 1 (
    echo ERROR: Dependency installation failed. Check network/package-index access and retry.
    echo Do not disable endpoint protection or weaken system security policy.
    exit /b 15
)

"%VENV_PY%" -c "from importlib.metadata import version; expected={'requests':'2.32.5','mutagen':'1.47.0','charset-normalizer':'3.4.9','idna':'3.18','urllib3':'2.7.0','certifi':'2026.6.17'}; raise SystemExit(0 if all(version(k)==v for k,v in expected.items()) else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Exact hash-locked dependencies are unavailable after local installation.
    exit /b 16
)

:dependencies_ready
> "%DEPS_MARKER%" echo Exact hash-checked dependencies verified for MediaTaggerBot v0.5.4 on %DATE% %TIME%
exit /b 0

:find_base_python
set "PY_EXE="
set "PY_SWITCH="

where py.exe >nul 2>nul
if not errorlevel 1 py -3.14 -c "import platform,sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) and platform.machine().lower() in {'amd64','x86_64'} else 1)" >nul 2>nul
if not errorlevel 1 set "PY_EXE=py"
if not errorlevel 1 set "PY_SWITCH=-3.14"
if defined PY_EXE exit /b 0

where py.exe >nul 2>nul
if not errorlevel 1 py -3.13 -c "import platform,sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) and platform.machine().lower() in {'amd64','x86_64'} else 1)" >nul 2>nul
if not errorlevel 1 set "PY_EXE=py"
if not errorlevel 1 set "PY_SWITCH=-3.13"
if defined PY_EXE exit /b 0

where py.exe >nul 2>nul
if not errorlevel 1 py -3.12 -c "import platform,sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) and platform.machine().lower() in {'amd64','x86_64'} else 1)" >nul 2>nul
if not errorlevel 1 set "PY_EXE=py"
if not errorlevel 1 set "PY_SWITCH=-3.12"
if defined PY_EXE exit /b 0

where py.exe >nul 2>nul
if not errorlevel 1 py -3.11 -c "import platform,sys; raise SystemExit(0 if sys.version_info[:2]==(3,11) and platform.machine().lower() in {'amd64','x86_64'} else 1)" >nul 2>nul
if not errorlevel 1 set "PY_EXE=py"
if not errorlevel 1 set "PY_SWITCH=-3.11"
if defined PY_EXE exit /b 0

where python.exe >nul 2>nul
if not errorlevel 1 python -c "import platform,sys; ok=sys.version_info[:2] in {(3,11),(3,12),(3,13),(3,14)} and platform.machine().lower() in {'amd64','x86_64'}; raise SystemExit(0 if ok else 1)" >nul 2>nul
if not errorlevel 1 set "PY_EXE=python"
if defined PY_EXE exit /b 0

echo ERROR: Supported 64-bit Python 3.11, 3.12, 3.13, or 3.14 was not found.
echo Install a supported 64-bit Python from python.org, then reopen this BAT.
exit /b 11

:askroot_optional
echo.
echo Press Enter to use the media root saved in config.toml.
echo Or enter a one-run override. Quotes are optional; drive roots such as D:\ are supported.
set /p "ROOT_ARG=Media root override: "
exit /b 0

:askroot_required
echo.
echo Enter the full folder containing your music/videos.
echo Examples: D:\Music   or   D:\   or   \\Server\Share\Music
echo Quotes are optional. The bot writes a TOML-safe literal path automatically.
set /p "ROOT_ARG=Media root path to save: "
if not defined ROOT_ARG (
    echo Set-root cancelled: no folder path entered.
    pause
    exit /b 1
)
exit /b 0

:askrollback
echo.
set /p "ROLLBACK_ARG=Path to rollback_manifest_*.json: "
if not defined ROLLBACK_ARG (
    echo Rollback cancelled: no manifest path entered.
    pause
    exit /b 1
)
exit /b 0

:confirmapply
echo.
echo WARNING: %~1 changes filenames and/or embedded metadata.
if /I "%~1"=="apply-all" echo WARNING: apply-all is intentionally aggressive and can use lower-confidence matches.
if /I "%~1"=="rollback" echo WARNING: rollback renames files back but does not undo embedded tag edits.
echo Backups are assumed.
set "CONFIRM="
set /p "CONFIRM=Type APPLY to continue: "
if /I not "%CONFIRM%"=="APPLY" (
    echo Cancelled by user.
    pause
    exit /b 1
)
exit /b 0

:editconfig
rem Manual config repair must remain available even when the full media runtime is broken.
rem Post-save validation is routed through the dependency-free control path.
if not exist "%PROJECT_ROOT%\config\config.toml" (
    if not exist "%PROJECT_ROOT%\config\config.example.toml" (
        echo Could not find config\config.example.toml.
        pause
        exit /b 1
    )
    copy /Y "%PROJECT_ROOT%\config\config.example.toml" "%PROJECT_ROOT%\config\config.toml" >nul
    if errorlevel 1 (
        echo Could not create config\config.toml.
        pause
        exit /b 1
    )
    echo Created config\config.toml from the shipped example for manual editing.
)
call :timestamp
set "BACKUP_FILE=%PROJECT_ROOT%\config\backups\config_before_manual_edit_%STAMP%.toml.bak"
copy /Y "%PROJECT_ROOT%\config\config.toml" "%BACKUP_FILE%" >nul
if errorlevel 1 (
    echo Could not create the pre-edit config backup.
    pause
    exit /b 1
)
echo Pre-edit backup: %BACKUP_FILE%
start "" /wait notepad.exe "%PROJECT_ROOT%\config\config.toml"
call :runmode validate-config "%BACKUP_FILE%"
exit /b 0

:openfolder
set "TARGET=%~1"
if not exist "%TARGET%" mkdir "%TARGET%" >nul 2>nul
start "" "%PROJECT_ROOT%\%TARGET%"
exit /b 0

:timestamp
set "STAMP="
for /f "tokens=2-4 delims=/ " %%A in ("%DATE%") do set "STAMP=%%C%%A%%B"
set "CLOCK=%TIME: =0%"
if defined STAMP set "STAMP=%STAMP%_%CLOCK:~0,2%%CLOCK:~3,2%%CLOCK:~6,2%"
if not defined STAMP set "STAMP=batch_%RANDOM%_%RANDOM%"
exit /b 0
