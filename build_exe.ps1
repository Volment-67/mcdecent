# Build a standalone windowed program (no console) with PyInstaller.
# Usage:  powershell -ExecutionPolicy Bypass -File C:\MC\build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/2] installing PyInstaller ..."
python -m pip install --quiet --upgrade pyinstaller

Write-Host "[2/2] building dist\mcdecent\mcdecent.exe ..."
python -m PyInstaller --noconfirm --clean --windowed --name mcdecent --onedir `
    --paths $PSScriptRoot --collect-submodules mcdecent `
    --add-data "$PSScriptRoot\mcdecent\assets;mcdecent\assets" `
    --icon "$PSScriptRoot\mcdecent\assets\app.ico" gui_launcher.py

$exe = Join-Path $PSScriptRoot "dist\mcdecent\mcdecent.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "OK: $exe" -ForegroundColor Green
    Write-Host "Double-click it to open the GUI launcher."
} else {
    Write-Error "build failed: $exe not found"
    exit 1
}
