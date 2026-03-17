@echo off
setlocal
if not exist .venv (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
pyinstaller --noconfirm --noconsole --name BodycamUploader app.py

echo.
echo EXE nahoditsya v papke dist\BodycamUploader\BodycamUploader.exe
pause
