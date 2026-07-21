# Splitify - Professional Lossless Video Splitter

**Splitify (SplitMaster)** is a 100% standalone, high-performance Windows desktop application designed to split video files into multiple segments instantly and without any quality loss. 

It harnesses FFmpeg's lossless stream copy engine under the hood and features a modern, responsive glassmorphism GUI running in native Microsoft Edge App Mode.

---

## ✨ Key Features

- 🚀 **100% Standalone & Portable**: No Python, no FFmpeg installation, and no configuration required for end users. FFmpeg and FFprobe engines are bundled directly inside a single `.exe` binary.
- 💻 **Native Desktop Window**: Runs in dedicated Microsoft Edge App Mode (`msedge --app`) without browser tabs, address bars, or `.dll` dependency conflicts.
- ⚡ **Lossless Stream Copying**: Splits videos in seconds using FFmpeg stream copy mode (`-c copy`) without re-encoding, preserving 100% original video and audio quality.
- 🎛️ **3 Flexible Split Modes**:
  - **Equal Parts**: Divide video into N equal duration segments.
  - **By Duration**: Split video into parts of fixed duration (e.g., every 15 minutes).
  - **Custom Ranges**: Define custom split points (timestamps) manually.
- ⏱️ **Boundary Overlap Time**: Configure overlap seconds (e.g. 5-10s) between consecutive parts so that no context is missed between video boundaries.
- 📊 **Real-Time Progress Monitoring**: Live Server-Sent Events (SSE) progress bar showing real-time percentage, current part counter, elapsed time, and ETA calculation.
- 🎬 **Built-in Media Player & Explorer Integration**: Preview split segments directly inside the application and open output folders natively in Windows Explorer.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.14, FastAPI, Uvicorn, FFmpeg & FFprobe
- **Frontend**: HTML5, Tailwind CSS, Lucide Icons, Google Fonts (Outfit & Kantumruy Pro)
- **Desktop Shell**: Microsoft Edge Native App Mode
- **Bundling Engine**: PyInstaller

---

## 📦 How to Use & Share

### For End Users
1. Locate the single compiled binary at `dist/Splitify.exe`.
2. Share `Splitify.exe` directly via Telegram, Google Drive, OneDrive, or USB Flash Drive.
3. Double-click `Splitify.exe` on any Windows 10/11 computer to launch the app instantly—no installation required!

### For Developers

#### 1. Clone the Repository
```bash
git clone https://github.com/PANHA006/SplitMaster.git
cd SplitMaster
```

#### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

#### 3. Run Development Server
```bash
python main.py
```
This starts the local FastAPI server and launches the desktop app in Edge App Mode.

#### 4. Run Integration Tests
```bash
python test_api.py
```
Executes an automated suite that generates synthetic video input, tests API endpoints (`/api/load-video`, `/api/split`), verifies real-time SSE progress streaming, and validates output files.

#### 5. Build Standalone Executable
```bash
python build.py
```
Runs the automated PyInstaller build script to bundle `main.py`, static assets (`public/`), `ffmpeg.exe`, and `ffprobe.exe` into a single binary at `dist/Splitify.exe`.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
