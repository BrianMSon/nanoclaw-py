@echo off

.venv\Scripts\python.exe -m PyInstaller nanoclaw.spec --noconfirm
copy dist\nanoclaw.exe nanoclaw.exe

pause