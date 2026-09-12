<#
    Builds the Prompt Maxxer installer from a clean checkout.

    Output: shell\src-tauri\target\release\bundle\nsis\Prompt Maxxer_<version>_x64-setup.exe

    Needs Python 3.12, Node.js with pnpm, Rust, and Visual Studio Build Tools
    with the C++ workload. People who only want to use the app need none of
    this - they install the release from GitHub.

    -SkipEngine  reuse an engine already frozen in engine\dist (shell-only changes)
#>
param(
    [switch]$SkipEngine
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$engine = Join-Path $root "engine"
$venvPython = Join-Path $engine ".venv\Scripts\python.exe"

function Step($message) { Write-Host "`n== $message ==" -ForegroundColor DarkYellow }

# Windows PowerShell turns whatever a native program writes to stderr into an
# error record, which aborts a script running with ErrorActionPreference Stop -
# even for pip's upgrade notice or PyInstaller's progress log, both of which go
# to stderr. Native tools run through this and are judged by exit code alone.
function Invoke-Native([string]$What, [scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

if (-not $SkipEngine) {
    Step "Engine environment"
    if (-not (Test-Path $venvPython)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Invoke-Native "creating the virtualenv" { py -3.12 -m venv (Join-Path $engine ".venv") }
        } else {
            Invoke-Native "creating the virtualenv" { python -m venv (Join-Path $engine ".venv") }
        }
    }
    Invoke-Native "upgrading pip" { & $venvPython -m pip install --quiet --upgrade pip }
    # Constrained to the lockfile, so the build uses the exact library versions
    # the engine was tested with, not whatever was released since.
    Invoke-Native "installing engine dependencies" {
        & $venvPython -m pip install --quiet -c (Join-Path $engine "requirements.lock") -e "$engine[cuda]" pyinstaller
    }

    Step "Freezing the engine"
    Push-Location $engine
    try {
        Invoke-Native "PyInstaller" {
            & $venvPython -m PyInstaller --noconfirm --log-level WARN --distpath dist --workpath build packaging\prompt-maxxer-engine.spec
        }
    } finally {
        Pop-Location
    }
}

$frozenEngine = Join-Path $engine "dist\prompt-maxxer-engine"
if (-not (Test-Path (Join-Path $frozenEngine "prompt-maxxer-engine.exe"))) {
    throw "No frozen engine in engine\dist. Run without -SkipEngine."
}

# NSIS cannot hold more than 2 GiB of uncompressed data, and when it overflows it
# fails with "Internal compiler error #12345: error mmapping file ... is out of
# range" - which says nothing about why. Catch it here with the actual reason.
$engineBytes = (Get-ChildItem $frozenEngine -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Frozen engine: {0:N2} GiB" -f ($engineBytes / 1GB))
if ($engineBytes -gt 1.95GB) {
    throw ("The frozen engine is {0:N2} GiB, over what an NSIS installer can hold (2 GiB). " +
           "Check what engine\packaging\prompt-maxxer-engine.spec is pulling in - large native " +
           "libraries belong in the first-launch download (cuda_runtime.py), not the bundle.") -f ($engineBytes / 1GB)
}

Step "App and installer"
# Installers are signed with the release key so installed copies accept them
# as updates. Without the key the build still works, but what it produces
# cannot be published with release.ps1.
$configs = @("src-tauri/tauri.bundle.conf.json")
$keyFile = Join-Path $env:USERPROFILE ".prompt-maxxer\updater.key"
$setKey = $false
if ($env:TAURI_SIGNING_PRIVATE_KEY) {
    Write-Host "Signing for updates with the key in TAURI_SIGNING_PRIVATE_KEY"
} elseif (Test-Path $keyFile) {
    Write-Host "Signing for updates with $keyFile"
    $env:TAURI_SIGNING_PRIVATE_KEY = (Get-Content -Raw $keyFile).Trim()
    $setKey = $true
} else {
    Write-Warning ("No update signing key at $keyFile. Building an unsigned installer, " +
                   "which works but cannot be published as an update.")
    $configs += "src-tauri/tauri.unsigned.conf.json"
}
$configArgs = foreach ($config in $configs) { "--config"; $config }

# Every dependency comes from a lockfile. pnpm has no way to pass --locked
# through to cargo, so the same guarantee comes from resolving the Rust
# dependencies first: this fails if Cargo.lock does not already describe them
# exactly, and the build that follows then has nothing left to resolve.
Invoke-Native "verifying Cargo.lock" {
    cargo fetch --locked --manifest-path (Join-Path $root "shell\src-tauri\Cargo.toml")
}

Push-Location (Join-Path $root "shell")
try {
    Invoke-Native "pnpm install" { pnpm install --frozen-lockfile }
    Invoke-Native "tauri build" { pnpm tauri build --ci @configArgs }
} finally {
    Pop-Location
    if ($setKey) { Remove-Item Env:\TAURI_SIGNING_PRIVATE_KEY }
}

$installer = Get-ChildItem (Join-Path $root "shell\src-tauri\target\release\bundle\nsis") -Filter "*-setup.exe" |
    Sort-Object LastWriteTime | Select-Object -Last 1
Write-Host ("`nInstaller: {0} ({1:N2} GB)" -f $installer.FullName, ($installer.Length / 1GB)) -ForegroundColor Green
if (Test-Path "$($installer.FullName).sig") { Write-Host "Update signature: $($installer.FullName).sig" -ForegroundColor Green }
