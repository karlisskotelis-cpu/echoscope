# Installs EchoScope for the current user (no admin needed):
#   copies dist\EchoScope.exe to %LOCALAPPDATA%\Programs\EchoScope\
#   adds a Start Menu shortcut (so Start search finds "EchoScope") and a Desktop shortcut.
# Every install first saves the currently installed EXE (and its settings) to previous\.
#
# Usage:
#   install.ps1                      install / update (backs up the current version to previous\)
#   install.ps1 -NoDesktop           same, without the Desktop shortcut
#   install.ps1 -Snapshot stable     keep a named copy of the CURRENT install as versions\stable\
#                                    with its own Start Menu shortcut "EchoScope (stable)"
#   install.ps1 -Revert              put the previous\ version back
#   install.ps1 -Revert -Snapshot stable   put the named snapshot back
#   install.ps1 -Uninstall           remove everything (snapshots included)
param([switch]$NoDesktop, [switch]$Uninstall, [switch]$Revert, [string]$Snapshot)
$ErrorActionPreference = 'Stop'

$src = Join-Path $PSScriptRoot 'dist\EchoScope.exe'
$dir = Join-Path $env:LOCALAPPDATA 'Programs\EchoScope'
$exe = Join-Path $dir 'EchoScope.exe'
$prevDir = Join-Path $dir 'previous'
$startDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$startMenu = Join-Path $startDir 'EchoScope.lnk'
$desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'EchoScope.lnk'

function Stop-Installed {
    Get-Process EchoScope -ErrorAction SilentlyContinue | ForEach-Object {
        try { if ($_.Path -like "$dir\*") { Stop-Process -Id $_.Id -Force } } catch {}
    }
    Start-Sleep -Milliseconds 400
}

function New-Shortcut($path, $target, $workdir, $desc) {
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $target
    $s.WorkingDirectory = $workdir
    $s.IconLocation = "$target,0"
    $s.Description = $desc
    $s.Save()
}

function Copy-Version($fromDir, $toDir) {
    New-Item -ItemType Directory -Force $toDir | Out-Null
    Copy-Item (Join-Path $fromDir 'EchoScope.exe') (Join-Path $toDir 'EchoScope.exe') -Force
    $s = Join-Path $fromDir 'settings.json'
    if (Test-Path $s) { Copy-Item $s (Join-Path $toDir 'settings.json') -Force }
}

if ($Uninstall) {
    Stop-Installed
    Remove-Item $startMenu, $desktop -ErrorAction SilentlyContinue
    Get-ChildItem $startDir -Filter 'EchoScope (*).lnk' -ErrorAction SilentlyContinue | Remove-Item -Force
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    Write-Host "Removed $dir and its shortcuts."
    return
}

if ($Revert) {
    $fromDir = if ($Snapshot) { Join-Path $dir "versions\$Snapshot" } else { $prevDir }
    if (-not (Test-Path (Join-Path $fromDir 'EchoScope.exe'))) { throw "Nothing saved at $fromDir" }
    Stop-Installed
    Copy-Version $fromDir $dir
    Write-Host "Reverted $exe to the copy in $fromDir"
    return
}

if ($Snapshot) {
    if (-not (Test-Path $exe)) { throw "Nothing installed yet at $exe" }
    $snapDir = Join-Path $dir "versions\$Snapshot"
    Copy-Version $dir $snapDir
    New-Shortcut (Join-Path $startDir "EchoScope ($Snapshot).lnk") (Join-Path $snapDir 'EchoScope.exe') $snapDir "EchoScope snapshot: $Snapshot"
    Write-Host "Saved the current version as $snapDir"
    Write-Host "Start Menu: 'EchoScope ($Snapshot)' runs it. Restore it with: install.ps1 -Revert -Snapshot $Snapshot"
    return
}

if (-not (Test-Path $src)) { throw "dist\EchoScope.exe not found. Run build.ps1 first." }
New-Item -ItemType Directory -Force $dir | Out-Null
Stop-Installed
if (Test-Path $exe) {
    Copy-Version $dir $prevDir
    New-Shortcut (Join-Path $startDir 'EchoScope (previous version).lnk') (Join-Path $prevDir 'EchoScope.exe') $prevDir 'EchoScope: the version before the last install'
}
Copy-Item $src $exe -Force

New-Shortcut $startMenu $exe $dir 'EchoScope: audio gaming radar'
if (-not $NoDesktop) { New-Shortcut $desktop $exe $dir 'EchoScope: audio gaming radar' }

Write-Host "Installed: $exe"
Write-Host "Start Menu: press the Windows key and type EchoScope."
Write-Host "settings.json and errorlog.txt live in $dir; the version before this one is in $prevDir"
