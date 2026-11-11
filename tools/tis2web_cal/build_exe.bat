@echo off
rem Builds dist\CAL PRO.exe  (needs: pip install playwright pyinstaller)
cd /d "%~dp0..\.."
python -m PyInstaller --noconfirm --onefile --windowed --name "CAL PRO" ^
  --icon "%CD%\tools\tis2web_cal\cal_pro.ico" ^
  --add-data "%CD%\tools\tis2web_cal\cal_pro.ico;." ^
  --paths tools\tis2web_cal --paths tools\vin_rpo --collect-all playwright ^
  --distpath dist --workpath build\tis2web_cal --specpath build ^
  tools\tis2web_cal\cal_pro_gui.pyw
