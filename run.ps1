<#
    Launches Prompt Maxxer.

    The app starts its own engine, so this is really just a convenience
    wrapper that also surfaces errors the windowed app would swallow. Use
    -Console to watch the engine log while debugging.
#>
param(
    [switch]$Console,
    [switch]$Settings
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$exe = Join-Path $root "shell\src-tauri\target\release\prompt-maxxer.exe"
$python = Join-Path $root "engine\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error @"
The engine virtualenv is missing. Create it with:

  cd $root\engine
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e ".[cuda]"
"@
}

if ($Console) {
    # Run the engine in this window so its log is visible.
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    Write-Host "Running the engine in the foreground. Ctrl+C to stop." -ForegroundColor DarkYellow
    & $python -m prompt_maxxer -v
    return
}

if (-not (Test-Path $exe)) {
    Write-Error "Prompt Maxxer is not built yet. Build it with: cd $root\shell; pnpm install; pnpm tauri build"
}

# Not $args: that is an automatic variable in PowerShell. And Start-Process
# rejects an empty -ArgumentList, so the no-argument case needs its own call.
if ($Settings) {
    Start-Process -FilePath $exe -ArgumentList "--settings"
} else {
    Start-Process -FilePath $exe
}

Write-Host "Prompt Maxxer is starting. The model takes a few seconds to load." -ForegroundColor Green
Write-Host "Hold Right Ctrl to dictate. Click the tray icon for settings." -ForegroundColor Green
