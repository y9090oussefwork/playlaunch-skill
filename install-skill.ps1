<#
.SYNOPSIS
    PlayLaunch — Universal Windows Installer for AI Agent Skills (Antigravity, Gemini, Codex, Claude, Cursor).
.DESCRIPTION
    Installs the `playlaunch` portable Agent Skill into standard Windows AI agent skill directories:
      - ~\.agents\skills\playlaunch
      - ~\.gemini\config\skills\playlaunch
      - ~\.gemini\antigravity\skills\playlaunch
      - ~\.codex\skills\playlaunch
      - ~\.claude\skills\playlaunch
    Uses NTFS Directory Junctions by default (zero admin privileges required, instant sync with git pull,
    and direct access to .venv/, stages/, core/, config/, and vendors/).
    Alternatively use `-Mode Copy` to copy lightweight SKILL.md + references/ with a pointer to SKILL_ROOT.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-skill.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-skill.ps1 -Target "C:\Users\Me\.cursor\skills\playlaunch"
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-skill.ps1 -Mode Copy -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet("Junction", "Copy")]
    [string]$Mode = "Junction",
    [string[]]$Target = @(),
    [switch]$OnlyCustomTargets,
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$SkillRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillMd = Join-Path $SkillRoot "SKILL.md"
if (-not (Test-Path $SkillMd)) {
    throw "SKILL.md not found in $SkillRoot. Run this script from the PlayLaunch_Studio root."
}

$DefaultTargets = @(
    (Join-Path $HOME ".agents\skills\playlaunch"),
    (Join-Path $HOME ".gemini\config\skills\playlaunch"),
    (Join-Path $HOME ".gemini\antigravity\skills\playlaunch"),
    (Join-Path $HOME ".codex\skills\playlaunch"),
    (Join-Path $HOME ".claude\skills\playlaunch")
)

$AllTargets = @()
if (-not $OnlyCustomTargets) {
    $AllTargets += $DefaultTargets
}
foreach ($t in $Target) {
    if ($t -and $t.Trim()) {
        $expanded = [Environment]::ExpandEnvironmentVariables($t.Trim())
        if ($expanded.StartsWith("~")) {
            $expanded = Join-Path $HOME $expanded.Substring(1).TrimStart("\", "/")
        }
        $AllTargets += $expanded
    }
}
$AllTargets = @($AllTargets | Select-Object -Unique)

Write-Host "=== PlayLaunch Portable Agent Skill Installer ===" -ForegroundColor Cyan
Write-Host "Skill Root : $SkillRoot"
Write-Host "Mode       : $Mode"
if ($DryRun) { Write-Host "DryRun     : ENABLED (no filesystem changes)" -ForegroundColor Yellow }

foreach ($dest in $AllTargets) {
    if ($Uninstall) {
        if (Test-Path $dest) {
            Write-Host "[UNINSTALL] Removing $dest" -ForegroundColor Yellow
            if (-not $DryRun) {
                $item = Get-Item -LiteralPath $dest -Force
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    cmd /c rmdir "$dest" | Out-Null
                } else {
                    Remove-Item -LiteralPath $dest -Recurse -Force
                }
            }
        } else {
            Write-Host "[SKIP] Not installed: $dest" -ForegroundColor DarkGray
        }
        continue
    }

    $parentDir = Split-Path -Parent $dest
    if (-not (Test-Path $parentDir)) {
        Write-Host "[MKDIR] $parentDir" -ForegroundColor DarkCyan
        if (-not $DryRun) {
            New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
        }
    }

    if (Test-Path $dest) {
        $existing = Get-Item -LiteralPath $dest -Force
        $isReparse = [bool]($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)
        if ($isReparse -and -not $Force -and $Mode -eq "Junction") {
            Write-Host "[OK] Already linked: $dest" -ForegroundColor Green
            continue
        }
        if (-not $Force) {
            Write-Host "[SKIP] Destination already exists (pass -Force to replace): $dest" -ForegroundColor Yellow
            continue
        }
        Write-Host "[REPLACE] Removing existing $dest" -ForegroundColor Yellow
        if (-not $DryRun) {
            if ($isReparse) {
                cmd /c rmdir "$dest" | Out-Null
            } else {
                Remove-Item -LiteralPath $dest -Recurse -Force
            }
        }
    }

    if ($Mode -eq "Junction") {
        Write-Host "[LINK] $dest -> $SkillRoot" -ForegroundColor Green
        if (-not $DryRun) {
            New-Item -ItemType Junction -Path $dest -Target $SkillRoot | Out-Null
        }
    } else {
        Write-Host "[COPY] Installing lightweight skill wrapper into $dest" -ForegroundColor Green
        if (-not $DryRun) {
            New-Item -ItemType Directory -Path $dest -Force | Out-Null
            Copy-Item -Path (Join-Path $SkillRoot "references") -Destination (Join-Path $dest "references") -Recurse -Force
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            $skillContent = [IO.File]::ReadAllText($SkillMd, $utf8NoBom)
            $resolvedSkillContent = $skillContent.Replace("<SKILL>", $SkillRoot)
            [IO.File]::WriteAllText((Join-Path $dest "SKILL.md"), $resolvedSkillContent, $utf8NoBom)
            [IO.File]::WriteAllText((Join-Path $dest "SKILL_ROOT.txt"), $SkillRoot + "`n", $utf8NoBom)
        }
    }
}

Write-Host ""
Write-Host "Done! You can now invoke the 'playlaunch' skill from inside any Android or Flutter project." -ForegroundColor Green
