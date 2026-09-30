# Splitify - Professional Lossless Video Splitter & Merger

**Splitify (SplitMaster)** is a 100% standalone, high-performance Windows desktop application designed to split and merge video files instantly and without quality loss. 

It harnesses FFmpeg's lossless stream copy engine under the hood and features a modern, responsive glassmorphism GUI running in native Microsoft Edge App Mode.

---

## ✨ Key Features

- 🚀 **100% Standalone & Portable**: No Python, no FFmpeg installation, and no configuration required for end users. FFmpeg and FFprobe engines are bundled directly inside a single `.exe` binary.
- 💻 **Native Desktop Window**: Runs in dedicated Microsoft Edge App Mode (`msedge --app`) without browser tabs, address bars, or `.dll` dependency conflicts.
- ⚡ **Lossless Stream Copying**: Splits and joins videos in seconds using FFmpeg stream copy mode (`-c copy`) without re-encoding, preserving 100% original video and audio quality.
- 🔗 **Lossless & Smart Video Merger (Large Batch Capable)**: Merge dozens to hundreds of videos (e.g. 380+ episodes) seamlessly without Windows command-line limit errors (`[WinError 206]`). Supports instant Lossless stream copying, Concat Demuxer transcoding, and Batch Chunking fallback.
- 🛡️ **Self-healing Lossless Merge**: if FFmpeg reports `Non-monotonic DTS` (episodes encoded by different tools with a different `start_time`/timebase), Splitify automatically retries with timestamp-repair flags, then with a lossless MPEG-TS remux, and finally falls back to Selective Auto-Fix — so one odd episode can no longer kill a 95-episode batch.
- 🎛️ **3 Flexible Split Modes**:
  - **Equal Parts**: Divide video into N equal duration segments.
  - **By Duration**: Split video into parts of fixed duration (e.g., every 15 minutes).
  - **Custom Ranges**: Define custom split points (timestamps) manually.
- 🧠 **Smart Silence Detection**: Snaps split boundaries to silent pauses to avoid cutting off spoken words.
- ⏱️ **Boundary Overlap Time**: Configure overlap seconds (e.g. 5-10s) between consecutive parts so that no context is missed between video boundaries.
- 📊 **Real-Time Progress Monitoring**: Live Server-Sent Events (SSE) progress bar showing real-time percentage, current part counter, elapsed time, and ETA calculation.
- 🎬 **Built-in Media Player & Explorer Integration**: Preview split or merged videos directly inside the application and open output folders natively in Windows Explorer.

---

## 🛠️ Tech Stack

- **Backend**: Python 3.12+, FastAPI, Uvicorn, FFmpeg & FFprobe
- **Frontend**: HTML5, Tailwind CSS, Lucide Icons (bundled locally), Google Fonts (Outfit & Kantumruy Pro)
- **Desktop Shell**: Native App Mode window via Google Chrome or Microsoft Edge (`--app`)
- **Bundling Engine**: PyInstaller

---

## 📂 Project Layout

```
SplitMaster/
├── main.py                  # FastAPI app + FFmpeg split/merge engine
├── ffmpeg_tools.py          # Shared ffmpeg/ffprobe binary resolver (no hardcoded paths)
├── build.py                 # PyInstaller one-file build script
├── test_api.py              # Split & merge integration tests
├── test_selective_autofix.py# Selective auto-fix / drift verification tests
├── test_helpers.py          # Shared helpers for the test scripts
├── run.bat                  # Dev launcher (python main.py)
├── public/                  # UI (index.html, favicon.svg, lucide.min.js)
├── output/                  # Default split/merge output (created at runtime)
└── temp/                    # Uploads, thumbnails, FFmpeg concat lists (created at runtime)
```

---

## 📦 How to Use & Share

### For End Users
1. Locate the single compiled binary at `dist/Splitify.exe`.
2. Share `Splitify.exe` directly via Telegram, Google Drive, OneDrive, or USB Flash Drive.
3. Double-click `Splitify.exe` on any Windows 10/11 computer to launch the app instantly—no installation required!
4. Split/merge results are saved to an `output/` folder created **next to the .exe** (change it any time from the UI).

> ℹ️ The app opens a native App-Mode window using Chrome when available, otherwise Microsoft Edge, otherwise your default browser.

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

#### 3. Run Application
* **Option A**: Double-click `run.bat` to launch immediately!
* **Option B (Command Line)**:
```bash
python main.py
```
This starts the local FastAPI server on port 8765 and launches the desktop app in Edge App Mode.

#### 4. Run Integration Tests
```bash
python test_api.py
```
Executes an automated suite that generates synthetic video input, tests API endpoints (`/api/load-video`, `/api/split`), verifies real-time SSE progress streaming, and validates output files.

```bash
python scan_timestamps.py "D:\MyEpisodes"
```
Diagnostics helper: probes every episode's `start_time`, timebase and container-vs-stream duration drift and
prints which files break a plain lossless concat (plus the one-line FFmpeg command to re-mux them).

```bash
python test_selective_autofix.py
```
Runs the selective auto-fix verification (A/V drift, mixed FPS/resolution, missing audio track).

```bash
python test_merge_timestamp_recovery.py
```
Reproduces the "Non-monotonic DTS" merge failure (episodes with a different `start_time` / timebase) and
verifies that the app now detects it, recovers automatically, never sticks on *processing*, and never
leaves a half-written output file behind.

> All three suites locate FFmpeg automatically (`FFMPEG_BIN` / `FFPROBE_BIN` env vars → `bin/` folder → system PATH → WinGet package cache).

#### 5. Build Standalone Executable
```bash
python build.py
```
Runs the automated PyInstaller build script to bundle `main.py`, static assets (`public/`), `ffmpeg.exe`, and `ffprobe.exe` into a single binary at `dist/Splitify.exe`.

The build **fails early** if FFmpeg/FFprobe cannot be found, so you never ship a binary that cannot cut video.

---

## 📄 License

Distributed under the MIT License. See [`LICENSE`](LICENSE) for more information.
