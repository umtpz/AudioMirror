# AudioMirror

Mirror your Windows audio output to multiple devices with per-channel control over volume, latency, and stereo direction.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

---

## What it does

AudioMirror captures any audio output on your system and clones it to up to 3 additional output devices simultaneously. Each output channel is independently configurable:

- **Reverse L/R** — swap left and right channels
- **Volume** — 0–150% per output
- **Latency** — 0–500ms delay per output (useful for room acoustics sync)
- **Reverse All shortcut** — toggle all channels' stereo direction with a keypress, with audio feedback

Settings are saved automatically and restored on next launch. The app runs in the system tray and starts mirroring automatically if the same devices are available.

---

## The Dummy Output Trick

The most powerful use case: set a **virtual/dummy audio device** (like [VB-Cable](https://vb-audio.com/Cable/) or Steam's virtual speakers) as your Windows default output. Then use AudioMirror to clone it to your real speakers.

This gives you full control over every aspect of the signal — including the primary output — without any OS-level limitations.

```
Windows default → VB-Cable (silent/dummy)
                      ↓
              AudioMirror captures
                 ↙           ↘
         Output 1            Output 2
     (Front speakers)    (Rear speakers)
      vol: 100%           vol: 85%
      reverse: off        reverse: on   ← L/R flipped for rear placement
      latency: 0ms        latency: 20ms ← room sync
```

This way you can build a **DIY surround sound setup** using two regular stereo soundbars — no dedicated surround system required.

---

## Requirements

- Windows 10/11
- Python 3.10+: https://python.org/downloads  
  *(Check "Add Python to PATH" during install)*

---

## Installation

```bash
pip install pyaudiowpatch customtkinter numpy pystray pillow keyboard
```

---

## Usage

```bash
python audiomirror.py
```

The app starts minimized to the system tray.

- **Double-click** the tray icon to open
- **Minimize** to send back to tray
- **Right-click** tray icon for quick controls

### Setup

1. Select your **Source** — the audio output to clone (e.g. VB-Cable)
2. Configure **Output 1** (always active) — select device, set volume, latency, reverse
3. Optionally enable **Output 2** and **Output 3** with their own settings
4. Click **Start**

All settings save automatically on every change.

### Reverse All Shortcut

Set a global keyboard shortcut to instantly flip the stereo direction of all active outputs simultaneously. Each output toggles independently based on its current state. A high beep confirms reverse is on, a low beep confirms it's off. Beep volume is adjustable.

---

## Building as .exe

No Python installation needed on the target machine:

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name AudioMirror audiomirror.py
python -m PyInstaller --onefile --windowed --name AudioMirror audiomirror.py
```

Output: `dist/AudioMirror.exe`

---

## DIY Surround Sound Setup

What you need:
- 2 stereo soundbars (or any two stereo output devices, bluetooth etc)

Steps:
1. Choose one default output (for the best route sound to a unused output)
2. Place one sound device in front, one behind you
3. In AudioMirror: Source = Selected output, Output 1 = front sound device, Output 2 = rear sound device
4. Enable **Reverse L/R** on the rear sound device (so left/right match your listening position)
5. Fine-tune volume and latency per output
6. Use the Reverse All shortcut to quickly flip front/rear stereo if needed

Result: spatial stereo without a dedicated surround system.

---

## Config

Settings are stored at:
```
%APPDATA%\AudioMirror\config.json
```

---

## License

MIT