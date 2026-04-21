param(
  [string]$Python = "py -3.11"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/4] Tworzenie/aktywacja venv"
if (!(Test-Path .venv)) {
  & $Python -m venv .venv
}

Write-Host "[2/4] Instalacja zależności runtime"
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\pip install -r requirements.txt

Write-Host "[3/4] Instalacja PyInstaller"
.\.venv\Scripts\pip install pyinstaller

Write-Host "[4/4] Budowanie EXE klienta GUI"
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name RadioWezelClientGUI --hidden-import sounddevice -m radio_wz.client.client_gui

Write-Host "Gotowe. EXE: dist\\RadioWezelClientGUI.exe"
