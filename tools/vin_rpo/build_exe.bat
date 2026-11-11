@echo off
rem Builds dist\VIN RPO Lookup.exe  (needs: pip install playwright pyinstaller)
cd /d "%~dp0..\.."
python -m PyInstaller --noconfirm --onefile --windowed --name "VIN RPO Lookup" ^
  --icon "%CD%\tools\vin_rpo\vin_rpo.ico" ^
  --add-data "%CD%\tools\vin_rpo\vin_rpo.ico;." ^
  --paths tools\vin_rpo --collect-all playwright ^
  --distpath dist --workpath build\vin_rpo --specpath build ^
  tools\vin_rpo\vin_rpo_gui.pyw
