#requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$FreshEnvironment
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "Windows packages must be built on Windows. PyInstaller is not a cross-compiler."
}

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$buildEnvironment = Join-Path $repositoryRoot ".build-venv"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"
$releaseDirectory = Join-Path $repositoryRoot "release"
$portableDirectory = Join-Path $releaseDirectory "Pocket Manga Editor"
$workDirectory = Join-Path $repositoryRoot "build\pyinstaller"
$specFile = Join-Path $repositoryRoot "packaging\PocketMangaEditor.spec"

Push-Location $repositoryRoot
try {
    if ($FreshEnvironment -and (Test-Path -LiteralPath $buildEnvironment)) {
        Write-Host "Removing the existing isolated build environment..." -ForegroundColor Cyan
        Remove-Item -LiteralPath $buildEnvironment -Recurse -Force
    }

    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        $pythonLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
        if ($null -eq $pythonLauncher) {
            throw "Python's Windows launcher (py.exe) was not found. Install Python 3.10 or newer first."
        }
        Write-Host "Creating the isolated build environment..." -ForegroundColor Cyan
        Invoke-Checked -FilePath $pythonLauncher.Source -Arguments @(
            "-3", "-m", "venv", $buildEnvironment
        )
    }

    Write-Host "Installing application and packaging requirements..." -ForegroundColor Cyan
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "-r", (Join-Path $repositoryRoot "requirements.txt"),
        "-r", (Join-Path $repositoryRoot "requirements-build.txt")
    )

    if (-not $SkipTests) {
        Write-Host "Running the complete test suite..." -ForegroundColor Cyan
        Invoke-Checked -FilePath $buildPython -Arguments @(
            "-m", "unittest", "discover", "-s", "tests", "-q"
        )
    }

    Write-Host "Building the portable Windows application..." -ForegroundColor Cyan
    Invoke-Checked -FilePath $buildPython -Arguments @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", $releaseDirectory,
        "--workpath", $workDirectory,
        $specFile
    )

    Copy-Item -LiteralPath (Join-Path $repositoryRoot "packaging\windows-portable-readme.txt") -Destination (Join-Path $portableDirectory "PORTABLE-README.txt") -Force

    $executablePath = Join-Path $portableDirectory "Pocket Manga Editor.exe"
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "PyInstaller finished without producing the expected executable: $executablePath"
    }
    $bundledAssets = Join-Path $portableDirectory "_internal\pocket_manga_editor\companion\assets"
    foreach ($assetName in @(
        "index.html",
        "styles.css",
        "app.js",
        "manifest.webmanifest",
        "icon.svg",
        "icon-180.png",
        "icon-512.png"
    )) {
        $assetPath = Join-Path $bundledAssets $assetName
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
            throw "The packaged Companion asset is missing: $assetPath"
        }
    }

    $architecture = if ($env:PROCESSOR_ARCHITECTURE) {
        $env:PROCESSOR_ARCHITECTURE.ToLowerInvariant()
    } else {
        "unknown-architecture"
    }
    $archivePath = Join-Path $releaseDirectory (
        "Pocket-Manga-Editor-Windows-{0}.zip" -f $architecture
    )
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    Write-Host "Creating the portable ZIP..." -ForegroundColor Cyan
    Compress-Archive -LiteralPath $portableDirectory -DestinationPath $archivePath -CompressionLevel Optimal

    Write-Host ""
    Write-Host "Build complete." -ForegroundColor Green
    Write-Host "Portable folder: $portableDirectory"
    Write-Host "Executable:      $executablePath"
    Write-Host "Distribution ZIP: $archivePath"
} finally {
    Pop-Location
}
