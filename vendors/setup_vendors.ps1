<#
.SYNOPSIS
    PlayLaunch Studio — One-click Windows setup script for unmodified open-source vendor projects.
.DESCRIPTION
    Clones/installs each open-source vendor into its own isolated subdirectory under vendors/
    WITHOUT modifying vendor source code:
      - vendors/google-play-scraper       (Node.js — facundoolano/google-play-scraper)
      - vendors/aso                       (Node.js — facundoolano/aso)
      - vendors/open-screenshot-generator (Project native — dotnetdreamer/open-screenshot-generator)
      - vendors/detekt                    (JVM CLI — official detekt-cli release JAR)
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipNpmInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$VendorsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $VendorsDir

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Resolve-JbrJavaHome {
    $candidates = @(
        "C:\Program Files\Android\Android Studio\jbr",
        (Join-Path $env:LOCALAPPDATA "Programs\Android Studio\jbr")
    )
    foreach ($candidate in $candidates) {
        $javaExe = Join-Path $candidate "bin\java.exe"
        if (Test-Path $javaExe) {
            return $candidate
        }
    }
    if ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME "bin\java.exe"))) {
        return $env:JAVA_HOME
    }
    return $null
}

function Clone-Or-UpdateRepo {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$RepoUrl,
        [Parameter(Mandatory = $true)][string]$TargetDir
    )

    if ((Test-Path $TargetDir) -and $Force) {
        Write-Host "Removing existing $Name (Force requested)..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $TargetDir
    }

    if (-not (Test-Path $TargetDir)) {
        Write-Host "Cloning $RepoUrl -> $TargetDir" -ForegroundColor Green
        git clone --depth 1 $RepoUrl $TargetDir
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to clone $RepoUrl"
        }
    } else {
        Write-Host "$Name already present at $TargetDir (updating fast-forward)..." -ForegroundColor DarkCyan
        git -C $TargetDir pull --ff-only 2>$null
    }
}

function Install-NodeDepsIfPresent {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$TargetDir
    )

    if ($SkipNpmInstall) {
        Write-Host "Skipping npm install for $Name (-SkipNpmInstall)." -ForegroundColor Yellow
        return
    }

    $pkgJson = Join-Path $TargetDir "package.json"
    if (Test-Path $pkgJson) {
        Write-Host "Installing npm dependencies for $Name..." -ForegroundColor Green
        Push-Location $TargetDir
        try {
            npm install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) {
                throw "npm install failed in $TargetDir"
            }
        } finally {
            Pop-Location
        }
    }
}

# Verify prerequisites
Write-Step "Checking prerequisites (Git, Node.js, Android Studio JBR)"
$gitVer = (git --version)
$nodeVer = (node -v)
$npmVer = (npm -v)
$jbrHome = Resolve-JbrJavaHome

Write-Host "Git   : $gitVer"
Write-Host "Node  : $nodeVer (npm $npmVer)"
if ($jbrHome) {
    Write-Host "JBR   : $jbrHome" -ForegroundColor Green
} else {
    Write-Host "JBR   : NOT FOUND (install Android Studio or set JAVA_HOME)" -ForegroundColor Yellow
}

# 1. facundoolano/google-play-scraper
Write-Step "1/4 Setting up google-play-scraper (Node.js)"
$gpsDir = Join-Path $VendorsDir "google-play-scraper"
Clone-Or-UpdateRepo -Name "google-play-scraper" -RepoUrl "https://github.com/facundoolano/google-play-scraper.git" -TargetDir $gpsDir
Install-NodeDepsIfPresent -Name "google-play-scraper" -TargetDir $gpsDir

# 2. facundoolano/aso
Write-Step "2/4 Setting up aso (Node.js)"
$asoDir = Join-Path $VendorsDir "aso"
Clone-Or-UpdateRepo -Name "aso" -RepoUrl "https://github.com/facundoolano/aso.git" -TargetDir $asoDir
Install-NodeDepsIfPresent -Name "aso" -TargetDir $asoDir

# 3. dotnetdreamer/open-screenshot-generator
Write-Step "3/4 Setting up open-screenshot-generator"
$osgDir = Join-Path $VendorsDir "open-screenshot-generator"
Clone-Or-UpdateRepo -Name "open-screenshot-generator" -RepoUrl "https://github.com/dotnetdreamer/open-screenshot-generator.git" -TargetDir $osgDir
if (Test-Path (Join-Path $osgDir "package.json")) {
    Install-NodeDepsIfPresent -Name "open-screenshot-generator" -TargetDir $osgDir
} elseif (Get-ChildItem -Path $osgDir -Filter "*.csproj" -Recurse -ErrorAction SilentlyContinue) {
    Write-Host "Detected .NET project in open-screenshot-generator." -ForegroundColor Cyan
    if (Get-Command dotnet -ErrorAction SilentlyContinue) {
        Push-Location $osgDir
        try {
            dotnet restore
        } finally {
            Pop-Location
        }
    } else {
        Write-Host "dotnet SDK not found in PATH; repo cloned unmodified for inspection." -ForegroundColor Yellow
    }
} else {
    Write-Host "open-screenshot-generator cloned unmodified (custom/native layout)." -ForegroundColor Cyan
}

# 4. detekt/detekt CLI (Official fat JAR release)
Write-Step "4/4 Setting up detekt CLI (JVM)"
$detektDir = Join-Path $VendorsDir "detekt"
New-Item -ItemType Directory -Path $detektDir -Force | Out-Null

$detektVersion = "1.23.8"
$detektJarName = "detekt-cli-$detektVersion-all.jar"
$detektJarPath = Join-Path $detektDir $detektJarName
$detektUrl = "https://github.com/detekt/detekt/releases/download/v$detektVersion/$detektJarName"

if ((Test-Path $detektJarPath) -and -not $Force) {
    Write-Host "Detekt CLI already present: $detektJarPath" -ForegroundColor DarkCyan
} else {
    Write-Host "Downloading official Detekt CLI v$detektVersion from GitHub..." -ForegroundColor Green
    $headers = @{ "User-Agent" = "PlayLaunch-Studio-Setup" }
    try {
        Invoke-WebRequest -Uri $detektUrl -OutFile $detektJarPath -Headers $headers -UseBasicParsing
    } catch {
        Write-Host "v$detektVersion download failed, falling back to v1.23.7..." -ForegroundColor Yellow
        $detektVersion = "1.23.7"
        $detektJarName = "detekt-cli-$detektVersion-all.jar"
        $detektJarPath = Join-Path $detektDir $detektJarName
        $detektUrl = "https://github.com/detekt/detekt/releases/download/v$detektVersion/$detektJarName"
        Invoke-WebRequest -Uri $detektUrl -OutFile $detektJarPath -Headers $headers -UseBasicParsing
    }
}

# Record active detekt jar metadata (outside vendor source)
$detektMeta = @{
    version = $detektVersion
    jar_file = $detektJarName
    source_url = $detektUrl
    installed_at = (Get-Date).ToString("o")
} | ConvertTo-Json -Depth 3
Set-Content -Path (Join-Path $detektDir "vendor_manifest.json") -Value $detektMeta -Encoding UTF8

# Verify Detekt runs using Android Studio JBR
if ($jbrHome) {
    $javaExe = Join-Path $jbrHome "bin\java.exe"
    Write-Host "Verifying Detekt CLI with Android Studio JBR ($javaExe)..." -ForegroundColor Cyan
    $detektOut = & $javaExe -jar $detektJarPath --version 2>&1
    Write-Host "Detekt version output: $detektOut" -ForegroundColor Green
}

Write-Step "Vendor setup complete!"
Write-Host "Installed modules under: $VendorsDir" -ForegroundColor Green
Write-Host "  - google-play-scraper"
Write-Host "  - aso"
Write-Host "  - open-screenshot-generator"
Write-Host "  - detekt ($detektJarName)"
