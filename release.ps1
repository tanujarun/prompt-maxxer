<#
    Publishes the installer build.ps1 made as a release, which every installed
    copy then downloads and installs on its next restart.

        .\release.ps1 -Notes "What changed, in a sentence or two"

    Before building a release, raise the version in shell\src-tauri\tauri.conf.json,
    shell\src-tauri\Cargo.toml and shell\package.json: installed copies only
    move to a higher version.

    Needs the GitHub CLI signed in with access to both repositories, and an
    installer built with the update signing key (see "Releasing" in the README).
    The installer and latest.json go to the public releases-only repository; the
    source commit is tagged in this one.
#>
param(
    [Parameter(Mandatory)] [string]$Notes,
    [string]$Repo = "tanujarun/prompt-maxxer-releases"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Windows PowerShell turns a native program's stderr - git's progress output,
# for one - into errors that abort the script. Judge these by exit code alone.
function Invoke-Native([string]$What, [scriptblock]$Command, [switch]$AllowFailure) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command 2>&1 | ForEach-Object { "$_" }
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) { throw "$What failed (exit code $LASTEXITCODE)" }
}

$shell = Join-Path $root "shell"
$version = (Get-Content -Raw (Join-Path $shell "src-tauri\tauri.conf.json") | ConvertFrom-Json).version
$cargoVersion = (Select-String -Path (Join-Path $shell "src-tauri\Cargo.toml") -Pattern '^version = "(.+)"' |
    Select-Object -First 1).Matches[0].Groups[1].Value
$npmVersion = (Get-Content -Raw (Join-Path $shell "package.json") | ConvertFrom-Json).version
if ($cargoVersion -ne $version -or $npmVersion -ne $version) {
    throw "Versions disagree: tauri.conf.json $version, Cargo.toml $cargoVersion, package.json $npmVersion."
}

$bundle = Join-Path $shell "src-tauri\target\release\bundle\nsis"
$installer = Join-Path $bundle "Prompt Maxxer_${version}_x64-setup.exe"
$signature = "$installer.sig"
if (-not (Test-Path $installer)) { throw "No installer for $version in $bundle. Run .\build.ps1 first." }
if (-not (Test-Path $signature)) {
    throw "The $version installer has no update signature. Rebuild with the signing key in place (see the README)."
}
if ((Get-Item $signature).LastWriteTime -lt (Get-Item $installer).LastWriteTime) {
    throw "The signature is older than the installer. Rebuild with .\build.ps1."
}

$dirty = git -C $root status --porcelain
if ($dirty) { throw "Commit your changes first, so the release tag points at the code that was built." }

Invoke-Native "checking for an existing release" { gh release view "v$version" --repo $Repo } -AllowFailure | Out-Null
if ($LASTEXITCODE -eq 0) { throw "v$version is already released. Raise the version and rebuild." }

# Asset names with spaces are rewritten by GitHub, so upload under a plain one
# and point latest.json at exactly that.
$staging = Join-Path ([System.IO.Path]::GetTempPath()) "prompt-maxxer-release-$version"
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
New-Item -ItemType Directory $staging | Out-Null
$assetName = "Prompt-Maxxer_${version}_x64-setup.exe"
$asset = Join-Path $staging $assetName
Copy-Item $installer $asset

$manifest = [ordered]@{
    version   = $version
    notes     = $Notes
    pub_date  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    platforms = [ordered]@{
        "windows-x86_64" = [ordered]@{
            signature = (Get-Content -Raw $signature).Trim()
            url       = "https://github.com/$Repo/releases/download/v$version/$assetName"
        }
    }
}
$manifestPath = Join-Path $staging "latest.json"
# No byte-order mark: the updater's JSON parser rejects one.
[System.IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding $false))

Write-Host "Publishing Prompt Maxxer $version to $Repo"
Invoke-Native "creating the release" {
    gh release create "v$version" $asset $manifestPath --repo $Repo --title "Prompt Maxxer $version" --notes $Notes
}
Invoke-Native "tagging the source" { git -C $root tag -a "v$version" -m "Prompt Maxxer $version" }
Invoke-Native "pushing the tag" { git -C $root push origin "v$version" }

Remove-Item -Recurse -Force $staging
Write-Host "`nReleased: https://github.com/$Repo/releases/tag/v$version" -ForegroundColor Green
Write-Host "Installed copies pick it up within six hours, or at their next launch after that."
