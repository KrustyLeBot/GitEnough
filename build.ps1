# Builds dist\GitEnough.exe (single file, no console).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
}
$py = ".\.venv\Scripts\python.exe"
& $py tools\make_icon.py
& $py -m PyInstaller --noconfirm --onefile --windowed --name GitEnough `
    --icon build\icon.ico `
    --hidden-import keyring.backends.Windows `
    --exclude-module tkinter `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (is GitEnough.exe still running?)" }
Write-Host "OK -> dist\GitEnough.exe"
