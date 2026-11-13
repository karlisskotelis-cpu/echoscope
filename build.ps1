# Builds native\dist\EchoScope.exe (single portable file) with PyInstaller.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Find-Python {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return "py" }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p -and $p.Source -notlike '*WindowsApps*') { return $p.Source }
    throw "Python 3.11+ not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH') or run: winget install --id Python.Python.3.12 --exact"
}

$python = Find-Python
Write-Host "Using $python"
& $python -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $python make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon build failed" }
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name EchoScope --icon echoscope.ico `
    --exclude-module tkinter --exclude-module unittest --exclude-module pydoc --exclude-module test `
    echoscope.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\EchoScope.exe"
