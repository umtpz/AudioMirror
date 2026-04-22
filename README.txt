AudioMirror — Setup Guide
==========================

REQUIREMENTS:
- Python 3.10+: https://python.org/downloads
  (Check "Add Python to PATH" during install!)

INSTALL (one time):
  pip install pyaudiowpatch customtkinter numpy pystray pillow

RUN:
  python audiomirror.py

USAGE:
- App starts minimized to system tray
- Double-click tray icon to open
- Select source (the output to clone) and secondary output
- Check "Reverse L/R" if needed
- Click Start — settings are saved automatically
- Next launch will auto-start with the same devices
- Minimize closes to tray, right-click tray icon to quit

OPTIONAL — build as .exe (no Python needed):
  pip install pyinstaller
  pyinstaller --onefile --windowed --name AudioMirror audiomirror.py
  Output: dist/AudioMirror.exe
