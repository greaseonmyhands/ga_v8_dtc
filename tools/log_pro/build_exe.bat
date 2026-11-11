@echo off
rem Builds dist\LOG PRO.exe as a 32-bit program (J2534 driver DLLs are 32-bit).
rem Needs 32-bit Python 3.12 and:  py -3.12-32 -m pip install pyinstaller
cd /d "%~dp0..\.."
py -3.12-32 -m PyInstaller --noconfirm --onefile --windowed --name "LOG PRO" ^
  --icon "%CD%\tools\log_pro\log_pro.ico" ^
  --add-data "%CD%\tools\log_pro\log_pro.ico;." ^
  --add-data "%CD%\tools\log_pro\log_pro_*.png;." ^
  --add-data "%CD%\tools\log_pro\examples;examples" ^
  --paths tools\log_pro --paths tools\j2534_can ^
  --distpath dist --workpath build\log_pro --specpath build ^
  tools\log_pro\log_pro.pyw
