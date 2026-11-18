@echo off
rem Builds dist\RPO PRO.exe  (needs: pip install playwright pyinstaller, in 64-bit Python)
rem Uses the py launcher's 64-bit Python 3: a 32-bit Python on PATH (e.g. one installed for
rem J2534 tools) would build an exe without Playwright that cannot do lookups.
cd /d "%~dp0..\.."
py -3-64 -c "import playwright, PyInstaller" || (
  echo 64-bit Python 3 with playwright and pyinstaller not found. & exit /b 1)
py -3-64 -m PyInstaller --noconfirm --onefile --windowed --name "RPO PRO" ^
  --icon "%CD%\tools\vin_rpo\vin_rpo.ico" ^
  --add-data "%CD%\tools\vin_rpo\vin_rpo.ico;." ^
  --paths tools\vin_rpo --collect-all playwright ^
  --distpath dist --workpath build\vin_rpo --specpath build ^
  tools\vin_rpo\vin_rpo_gui.pyw
