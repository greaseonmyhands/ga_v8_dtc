@echo off
rem Builds dist\CAN PRO.exe as a 32-bit program (J2534 driver DLLs are 32-bit).
rem Needs 32-bit Python 3.12 and:  py -3.12-32 -m pip install pyinstaller
cd /d "%~dp0..\.."
py -3.12-32 -m PyInstaller --noconfirm --onefile --windowed --name "CAN PRO" ^
  --icon "%CD%\tools\j2534_can\can_pro.ico" ^
  --add-data "%CD%\tools\j2534_can\can_pro.ico;." ^
  --add-data "%CD%\tools\j2534_can\examples;examples" ^
  --paths tools\j2534_can ^
  --distpath dist --workpath build\j2534_can --specpath build ^
  tools\j2534_can\can_pro.pyw
