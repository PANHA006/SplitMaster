# Splitify - Professional Video Splitter

Splitify is a lightweight, high-performance desktop application designed to split video files into multiple segments quickly and without quality loss. It uses FFmpeg's lossless stream copy under the hood and features a modern glassmorphism GUI.

## Features

- **Lossless Splitting**: Splits videos in seconds using FFmpeg copy mode without re-encoding, preserving 100% original video and audio quality.
- **Multiple Split Modes**:
  - **Equal Parts**: Divide video into N equal duration segments.
  - **By Duration**: Split video into parts of fixed duration (e.g., every 15 minutes).
  - **Custom Ranges**: Define custom split points (timestamps) manually.
- **Overlap Time**: Configure overlap seconds (e.g., 5-10s) between parts so that no content is missed between video boundaries.
- **Native Desktop Integration**: Opens native Windows File Open and Folder Browser dialogs to choose inputs and output folders.
- **Local Browserless App**: Packaged into a standalone `.exe` that launches in a native desktop window frame (via Microsoft Edge WebView2).

## Tech Stack

- **Backend**: FastAPI (Python), Uvicorn, FFmpeg
- **Frontend**: HTML5, Tailwind CSS, Lucide Icons, Vanilla JavaScript
- **Desktop Wrapper**: PyWebview
- **Packaging**: PyInstaller

## Prerequisites

- **Python 3.10+**
- **FFmpeg**: Must be installed and added to your system's `PATH` environment variable.

## Setup & Local Development

1. **Clone the repository** (or navigate to project directory).
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Run the application**:
   ```bash
   python main.py
   ```
   This will spin up the local FastAPI server in the background and launch a native desktop GUI frame.

## Packaging as Standalone Executable (.exe)

You can package this project into a single standalone `.exe` file that does not show a console window on startup and runs like a native desktop app:

1. **Run the build script**:
   ```bash
   python build.py
   ```
2. Once compilation finishes successfully, your standalone executable will be located in the `dist/` directory:
   - **File**: `dist/Splitify.exe`
3. Double-click `Splitify.exe` to run the app instantly on any compatible Windows machine.
