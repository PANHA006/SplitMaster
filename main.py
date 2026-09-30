import os
import sys
import io
import subprocess
import threading
import time
import json
import math
import shutil
import re
from typing import Optional

# Fix PyInstaller --noconsole mode where sys.stdout and sys.stderr are None
if sys.stdout is None:
    sys.stdout = io.StringIO()
elif hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.stderr is None:
    sys.stderr = io.StringIO()
elif hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def log_terminal(msg: str):
    """Print clean formatted status log to the terminal."""
    try:
        now_str = time.strftime("%H:%M:%S")
        print(f"[{now_str}] [Splitify] {msg}", flush=True)
    except Exception:
        pass

# Silence harmless Windows WinError 10054 when browser closes/refreshes connection abruptly
if sys.platform == "win32":
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
        _orig_call_connection_lost = _ProactorBasePipeTransport._call_connection_lost

        def _silent_call_connection_lost(self, exc=None):
            try:
                _orig_call_connection_lost(self, exc)
            except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
                if getattr(e, "winerror", None) in (10054, 10053, 10038) or isinstance(e, (ConnectionResetError, ConnectionAbortedError)):
                    pass
                else:
                    raise

        _ProactorBasePipeTransport._call_connection_lost = _silent_call_connection_lost
    except Exception:
        pass

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ffmpeg_tools import find_binary

app = FastAPI(title="Splitify Backend")

# Enable CORS: only same-machine origins (the UI runs on 127.0.0.1 with a dynamic port)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State for video splitting task
split_state = {
    "is_running": False,
    "status": "idle",           # idle | processing | completed | canceled | error
    "task_type": "split",       # split | merge
    "progress_percent": 0,
    "current_part": 0,
    "total_parts": 0,
    "output_files": [],
    "output_folder": "",
    "error_message": "",
    "start_time": 0.0,
    "elapsed_time": 0.0,
    "eta_seconds": 0,
    "speed": "1.0",
    "fps": "",
    "active_process": None
}

# Folders the user explicitly worked with this session (used to validate media/folder requests)
allowed_media_dirs: set[str] = set()

# Thread lock for state updates
state_lock = threading.Lock()

# Models
class LoadVideoRequest(BaseModel):
    path: str

class SplitPart(BaseModel):
    partNum: int
    start: float
    end: float
    duration: float

class SplitRequest(BaseModel):
    videoPath: str
    mode: str  # parts, duration, custom
    overlapSec: Optional[int] = 0
    outputFolder: str
    outputPrefix: str
    parts: list[SplitPart]

class SmartSilenceRequest(BaseModel):
    videoPath: str
    targetPoints: list[float]
    searchWindow: Optional[float] = 20.0
    totalDuration: Optional[float] = 0.0

class MergeCheckRequest(BaseModel):
    videoPaths: list[str]

class MergeRequest(BaseModel):
    videoPaths: list[str]
    outputFolder: str
    outputFilename: str
    mode: Optional[str] = "auto"  # auto, lossless, reencode
    enableMultiPart: Optional[bool] = False
    partitionMode: Optional[str] = "fps"  # fps, count, parts, custom
    episodesPerPart: Optional[int] = 20
    targetPartCount: Optional[int] = 3
    customRanges: Optional[str] = ""

class PartitionPreviewRequest(BaseModel):
    videoPaths: list[str]
    partitionMode: Optional[str] = "fps"
    episodesPerPart: Optional[int] = 20
    targetPartCount: Optional[int] = 3
    customRanges: Optional[str] = ""
    outputFilename: Optional[str] = "Merged_Video"

class OpenFolderRequest(BaseModel):
    folder: Optional[str] = ""

# Determine base directory for resources
if getattr(sys, 'frozen', False):
    # Running in a PyInstaller bundle
    base_dir = sys._MEIPASS
else:
    # Running in a normal Python environment
    base_dir = os.path.dirname(os.path.abspath(__file__))

public_dir = os.path.join(base_dir, "public")

# Served static assets (index.html, favicon, lucide icons...) that ship with the app
SERVED_STATIC_FILES = ("index.html", "favicon.svg", "lucide.min.js")

# Video extensions accepted for upload / preview
ALLOWED_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m4v", ".flv", ".mpg", ".mpeg", ".wmv"}

# --- Lossless merge strategies (used by the retry chain) ------------------------------
# Attempt 1: plain stream copy, fastest, works when every episode shares the same timeline
LOSSLESS_BASE_FLAGS = ("-fflags", "+genpts", "-avoid_negative_ts", "make_zero")
# Attempt 2: stream copy + timestamp repair (fixes most "Non-monotonic DTS" failures)
LOSSLESS_REPAIR_FLAGS = (
    "-fflags", "+genpts+igndts",
    "-avoid_negative_ts", "make_zero",
    "-max_interleave_delta", "0",
    "-muxdelay", "0",
)
# Attempt 3: losslessly remux every episode to MPEG-TS first, then concat (always gives a
# clean continuous timeline, e.g. when episodes were produced by different tools/timebases)
TS_VIDEO_BSF = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb", "h265": "hevc_mp4toannexb"}

def explain_ffmpeg_error(raw: str) -> str:
    """Return a friendly Khmer explanation for well-known FFmpeg failures."""
    text = (raw or "").lower()
    if "non-monotonic dts" in text or "invalid data found when processing input" in text:
        return ("វីដេអូខ្លះមាន timestamp មិនស៊ីគ្នា (Non-monotonic DTS) ដូច្នេះមិនអាចភ្ជាប់បែប Lossless "
                "បានផ្ទាល់ឡើយ។ សូមសាកម្ដងទៀតដោយជ្រើស Mode “Auto / Selective Auto-Fix” ឬ “Full Re-encode”។")
    if "no space left" in text:
        return "Disk ពេញ មិនអាចសរសេរឯកសារលទ្ធផលបានឡើយ។ សូមបញ្ចេញទំហំរួចសាកម្ដងទៀត។"
    if "permission denied" in text or "access is denied" in text:
        return "គ្មានសិទ្ធិសរសេរក្នុង Folder លទ្ធផលឡើយ។ សូមជ្រើស Folder ផ្សេង (ឧ. Desktop)។"
    if "no such file or directory" in text or "cannot find" in text:
        return "រកមិនឃើញឯកសារ Input ឡើយ។ សូមត្រួតពិនិត្យផ្លូវឯកសារម្ដងទៀត។"
    if "invalid argument" in text:
        return "FFmpeg បដិសេធប៉ារ៉ាម៉ែត្រខ្លះ។ សូមសាកម្ដងទៀតដោយ Mode Re-encode។"
    return ""


def get_app_dir() -> str:
    """Writable root that persists next to the app (folder of the .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def _ensure_dir(path: str) -> str:
    """Create a folder next to the app; fall back to %LOCALAPPDATA%\\Splitify when not writable."""
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        fallback = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Splitify", os.path.basename(path)
        )
        os.makedirs(fallback, exist_ok=True)
        log_terminal(f"[WARN] '{path}' is not writable, using '{fallback}' instead.")
        return fallback

def get_data_dir() -> str:
    """Persistent folder for split / merge outputs."""
    return _ensure_dir(os.path.join(get_app_dir(), "output"))

def get_temp_dir() -> str:
    """Writable scratch folder for uploads, thumbnails and FFmpeg concat lists."""
    return _ensure_dir(os.path.join(get_app_dir(), "temp"))

def register_media_dir(path: str):
    """Remember a folder the user legitimately interacted with (for media validation)."""
    if not path:
        return
    try:
        folder = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    except Exception:
        return
    if os.path.isdir(folder):
        allowed_media_dirs.add(os.path.normcase(folder))

def is_path_allowed(path: str) -> bool:
    """True when the path sits inside a folder the user has already used this session."""
    try:
        target = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    for folder in allowed_media_dirs:
        if target == folder or target.startswith(folder + os.sep):
            return True
    return False

# Make sure the folders we write to exist on startup
get_data_dir()
get_temp_dir()
register_media_dir(get_data_dir())
register_media_dir(get_temp_dir())

# Helper to format seconds to hh:mm:ss
def format_seconds(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}"

def get_ffmpeg_cmd() -> str:
    """Resolve the ffmpeg binary (shared resolver: env -> bundle -> bin/ -> PATH -> WinGet)."""
    return find_binary("ffmpeg") or "ffmpeg"

def get_ffprobe_cmd() -> str:
    """Resolve the ffprobe binary (shared resolver: env -> bundle -> bin/ -> PATH -> WinGet)."""
    return find_binary("ffprobe") or "ffprobe"

def find_silence_cut_point(video_path: str, target_sec: float, search_window: float = 20.0, total_duration: float = 0.0) -> float:
    start_search = max(0.0, target_sec - search_window)
    end_search = target_sec + search_window
    if total_duration > 0.0:
        end_search = min(total_duration, end_search)
        
    duration_search = end_search - start_search
    if duration_search <= 0.5:
        return target_sec

    cmd = [
        get_ffmpeg_cmd(), "-y",
        "-ss", f"{start_search:.3f}",
        "-t", f"{duration_search:.3f}",
        "-i", video_path,
        "-af", "silencedetect=noise=-30dB:d=0.4",
        "-f", "null", "-"
    ]
    
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="ignore")
        intervals = []
        current_start = None
        
        for line in res.stderr.splitlines():
            if "silence_start:" in line:
                m = re.search(r"silence_start:\s*([0-9.]+)", line)
                if m:
                    current_start = float(m.group(1))
            elif "silence_end:" in line and current_start is not None:
                m = re.search(r"silence_end:\s*([0-9.]+)", line)
                if m:
                    end_val = float(m.group(1))
                    intervals.append((current_start, end_val))
                    current_start = None

        if current_start is not None:
            intervals.append((current_start, duration_search))

        if not intervals:
            return target_sec

        # Compute midpoints in absolute time: (start_search + s + start_search + e) / 2
        midpoints = []
        for s, e in intervals:
            abs_s = start_search + s
            abs_e = start_search + e
            mid = (abs_s + abs_e) / 2.0
            midpoints.append(mid)

        # Pick midpoint closest to target_sec
        best_midpoint = min(midpoints, key=lambda m: abs(m - target_sec))
        return round(best_midpoint, 3)

    except Exception as e:
        print(f"[WARN] Silence detection failed: {e}")
        return target_sec

@app.post("/api/smart-silence-points")
def get_smart_silence_points(req: SmartSilenceRequest):
    video_path = req.videoPath.strip()
    if (video_path.startswith('"') and video_path.endswith('"')) or (video_path.startswith("'") and video_path.endswith("'")):
        video_path = video_path[1:-1]
        
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារវីដេអូឡើយ។")

    results = []
    prev_point = 0.0
    for target in req.targetPoints:
        snapped = find_silence_cut_point(video_path, target, req.searchWindow or 20.0, req.totalDuration or 0.0)
        # Ensure snapped cut point stays ahead of previous point by at least 1s
        if snapped <= prev_point + 1.0:
            snapped = target
        results.append(snapped)
        prev_point = snapped

    return {
        "success": True,
        "cutPoints": results
    }

@app.get("/api/config")
def get_config():
    return {
        "default_output_folder": get_data_dir()
    }

def choose_file_dialog() -> str:
    # 1. Try native Tkinter with topmost
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        root.update()
        file_path = filedialog.askopenfilename(
            parent=root,
            title="ជ្រើសរើសឯកសារវីដេអូ (Select Video File)",
            filetypes=[
                ("Video Files", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v *.wmv"),
                ("All Files", "*.*")
            ]
        )
        root.destroy()
        if file_path:
            return os.path.normpath(file_path)
    except Exception as e:
        print(f"[WARN] Tkinter file dialog failed: {e}")

    # 2. Fallback to PowerShell TopMost
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; "
            "$f.Size = New-Object System.Drawing.Size(1, 1); "
            "$f.StartPosition = 'CenterScreen'; "
            "$f.TopMost = $true; "
            "$f.Opacity = 0; "
            "$f.Show(); "
            "$f.BringToFront(); "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            "$d.Filter = 'Video Files|*.mp4;*.mkv;*.avi;*.mov;*.webm;*.flv;*.ts;*.m4v;*.wmv|All Files|*.*'; "
            "$d.Title = 'ជ្រើសរើសឯកសារវីដេអូ (Select Video File)'; "
            "$res = $d.ShowDialog($f); "
            "$f.Close(); "
            "$f.Dispose(); "
            "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.FileName }"
        )
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
        file_path = subprocess.check_output(cmd, encoding="utf-8", errors="replace").strip()
        if file_path and os.path.exists(file_path):
            return os.path.normpath(file_path)
    except Exception as e:
        print(f"[WARN] PowerShell file dialog failed: {e}")
    return ""

def choose_folder_dialog() -> str:
    # 1. Try native Tkinter with topmost
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        root.update()
        folder_path = filedialog.askdirectory(
            parent=root,
            title="ជ្រើសរើសថតលទ្ធផល (Select Output Folder)"
        )
        root.destroy()
        if folder_path:
            return os.path.normpath(folder_path)
    except Exception as e:
        print(f"[WARN] Tkinter folder dialog failed: {e}")

    # 2. Fallback to PowerShell TopMost
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; "
            "$f.Size = New-Object System.Drawing.Size(1, 1); "
            "$f.StartPosition = 'CenterScreen'; "
            "$f.TopMost = $true; "
            "$f.Opacity = 0; "
            "$f.Show(); "
            "$f.BringToFront(); "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'ជ្រើសរើសថតលទ្ធផល (Select Output Folder)'; "
            "$res = $d.ShowDialog($f); "
            "$f.Close(); "
            "$f.Dispose(); "
            "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
        )
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
        folder_path = subprocess.check_output(cmd, encoding="utf-8", errors="replace").strip()
        if folder_path and os.path.exists(folder_path):
            return os.path.normpath(folder_path)
    except Exception as e:
        print(f"[WARN] PowerShell folder dialog failed: {e}")
    return ""

def choose_multiple_files_dialog() -> list[str]:
    # 1. Try native Tkinter with topmost
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        root.update()
        file_paths = filedialog.askopenfilenames(
            parent=root,
            title="ជ្រើសរើសឯកសារវីដេអូច្រើន (Select Multiple Video Files)",
            filetypes=[
                ("Video Files", "*.mp4 *.mkv *.avi *.mov *.webm *.flv *.ts *.m4v *.wmv"),
                ("All Files", "*.*")
            ]
        )
        root.destroy()
        if file_paths:
            return [os.path.normpath(p) for p in file_paths]
    except Exception as e:
        print(f"[WARN] Tkinter multi-file dialog failed: {e}")

    # 2. Fallback to PowerShell TopMost
    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; "
            "$f.Size = New-Object System.Drawing.Size(1, 1); "
            "$f.StartPosition = 'CenterScreen'; "
            "$f.TopMost = $true; "
            "$f.Opacity = 0; "
            "$f.Show(); "
            "$f.BringToFront(); "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; "
            "$d.Multiselect = $true; "
            "$d.Filter = 'Video Files|*.mp4;*.mkv;*.avi;*.mov;*.webm;*.flv;*.ts;*.m4v;*.wmv|All Files|*.*'; "
            "$d.Title = 'ជ្រើសរើសឯកសារវីដេអូច្រើន (Select Multiple Video Files)'; "
            "$res = $d.ShowDialog($f); "
            "$f.Close(); "
            "$f.Dispose(); "
            "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output ($d.FileNames -join '|') }"
        )
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
        raw_output = subprocess.check_output(cmd, encoding="utf-8", errors="replace").strip()
        if raw_output:
            paths = [os.path.normpath(p.strip()) for p in raw_output.split('|') if p.strip()]
            valid_paths = [p for p in paths if os.path.exists(p)]
            if valid_paths:
                return valid_paths
    except Exception as e:
        print(f"[WARN] PowerShell multi-file dialog failed: {e}")
    return []

@app.post("/api/select-file")
def select_file():
    try:
        path = choose_file_dialog()
        if path and os.path.exists(path):
            register_media_dir(path)
            return {"success": True, "path": path}
        return {"success": True, "path": ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open file dialog: {str(e)}")

@app.post("/api/select-multiple-files")
def select_multiple_files():
    try:
        paths = choose_multiple_files_dialog()
        for p in paths:
            register_media_dir(p)
        return {"success": True, "paths": paths}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open multi-file dialog: {str(e)}")

@app.post("/api/select-folder")
def select_folder():
    try:
        path = choose_folder_dialog()
        if path and os.path.exists(path):
            register_media_dir(path)
            return {"success": True, "path": path}
        return {"success": True, "path": ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open folder dialog: {str(e)}")

@app.post("/api/upload")
def upload_file(file: UploadFile = File(...)):
    try:
        safe_name = os.path.basename(file.filename or "").strip()
        ext = os.path.splitext(safe_name)[1].lower()
        if not safe_name or ext not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(status_code=400, detail="ប្រភេទឯកសារនេះមិនត្រូវបានអនុញ្ញាតឡើយ។")

        temp_dir = get_temp_dir()

        # Clean up old uploaded files in temp dir to save disk space
        for f in os.listdir(temp_dir):
            if os.path.splitext(f)[1].lower() in ALLOWED_VIDEO_EXTS or f == "thumbnail.jpg":
                try:
                    os.remove(os.path.join(temp_dir, f))
                except Exception:
                    pass

        temp_path = os.path.join(temp_dir, safe_name)
        with open(temp_path, "wb") as buffer:
            # Read in chunks of 1MB to handle large files efficiently
            while chunk := file.file.read(1024 * 1024):
                buffer.write(chunk)

        register_media_dir(temp_path)
        log_terminal(f"Uploaded Video: {safe_name}")
        return {"success": True, "path": os.path.abspath(temp_path)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"កំហុសក្នុងការ Upload វីដេអូ៖ {str(e)}")

@app.post("/api/load-video")
def load_video(req: LoadVideoRequest):
    video_path = req.path.strip()
    
    # Remove surrounding quotes if pasted by user
    if (video_path.startswith('"') and video_path.endswith('"')) or (video_path.startswith("'") and video_path.endswith("'")):
        video_path = video_path[1:-1]
        
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារវីដេអូតាមផ្លូវដែលបានបញ្ជាក់ឡើយ។")
        
    if not os.path.isfile(video_path):
        raise HTTPException(status_code=400, detail="ផ្លូវដែលបានបញ្ចូលមិនមែនជាឯកសារវីដេអូឡើយ។")

    try:
        # 1. Get Duration using ffprobe
        duration_cmd = [
            get_ffprobe_cmd(), "-v", "error", 
            "-show_entries", "format=duration", 
            "-of", "default=noprint_wrappers=1:nokey=1", 
            video_path
        ]
        duration_out = subprocess.check_output(duration_cmd, encoding="utf-8", errors="replace").strip()
        duration = float(duration_out)

        # 2. Get Resolution using ffprobe
        res_cmd = [
            get_ffprobe_cmd(), "-v", "error", 
            "-select_streams", "v:0", 
            "-show_entries", "stream=width,height", 
            "-of", "csv=s=x:p=0", 
            video_path
        ]
        resolution = subprocess.check_output(res_cmd, encoding="utf-8", errors="replace").strip()

        # 3. Get File Size
        size_bytes = os.path.getsize(video_path)
        size_gb = size_bytes / (1024 * 1024 * 1024)
        size_text = f"{size_gb:.2f} GB" if size_gb >= 1.0 else f"{size_bytes / (1024 * 1024):.2f} MB"

        # 4. Generate Thumbnail (at 5s mark or 0s if short)
        thumb_secs = 5.0 if duration >= 5.0 else 0.0
        thumb_dest = os.path.join(get_temp_dir(), "thumbnail.jpg")

        # Delete old thumbnail if exists
        if os.path.exists(thumb_dest):
            try:
                os.remove(thumb_dest)
            except Exception:
                pass

        thumb_cmd = [
            get_ffmpeg_cmd(), "-y", 
            "-ss", str(thumb_secs), 
            "-i", video_path, 
            "-vframes", "1", 
            "-q:v", "2", 
            thumb_dest
        ]
        # Run silently
        subprocess.run(thumb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        filename = os.path.basename(video_path)
        register_media_dir(video_path)
        log_terminal(f"Loaded Video: {filename} ({resolution}, {format_seconds(duration)}, {size_text})")

        return {
            "success": True,
            "filename": filename,
            "duration": duration,
            "duration_formatted": format_seconds(duration),
            "resolution": resolution,
            "size_text": size_text,
            "thumbnail_url": f"/temp/thumbnail.jpg?t={int(time.time())}"  # Cache buster
        }

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"កំហុសក្នុងការអានព័ត៌មានវីដេអូ៖ {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"កំហុសប្រព័ន្ធ៖ {str(e)}")

def format_fps(fps_str: str) -> tuple[str, float]:
    if not fps_str or fps_str == "0/0":
        return "Unknown", 0.0
    if "/" in fps_str:
        try:
            num, den = fps_str.split("/")
            den_f = float(den)
            if den_f == 0: return "Unknown", 0.0
            val = float(num) / den_f
            return f"{val:.2f} fps", round(val, 2)
        except Exception:
            return fps_str, 0.0
    try:
        val = float(fps_str)
        return f"{val:.2f} fps", round(val, 2)
    except Exception:
        return fps_str, 0.0

def probe_single_video(video_path: str) -> dict:
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"មិនរកឃើញឯកសារ៖ {video_path}")
    
    # 1. Duration + start_time (start_time matters for lossless concat)
    format_cmd = [
        get_ffprobe_cmd(), "-v", "error",
        "-show_entries", "format=duration,start_time",
        "-of", "json",
        video_path
    ]
    try:
        format_data = json.loads(subprocess.check_output(format_cmd, encoding="utf-8", errors="replace").strip() or "{}").get("format", {})
    except Exception:
        format_data = {}
    duration_str = format_data.get("duration", "")
    try:
        duration = float(duration_str) if duration_str else 0.0
    except Exception:
        duration = 0.0
    try:
        start_time = float(format_data.get("start_time") or 0.0)
    except Exception:
        start_time = 0.0

    # 2. Streams info (video & audio)
    streams_cmd = [
        get_ffprobe_cmd(), "-v", "error", 
        "-show_entries", "stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,has_b_frames,time_base,nb_frames", 
        "-of", "json", 
        video_path
    ]
    streams_out = subprocess.check_output(streams_cmd, encoding="utf-8", errors="replace").strip()
    streams_data = json.loads(streams_out).get("streams", [])

    v_stream = next((s for s in streams_data if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams_data if s.get("codec_type") == "audio"), None)

    width = int(v_stream.get("width", 0)) if v_stream and v_stream.get("width") else 0
    height = int(v_stream.get("height", 0)) if v_stream and v_stream.get("height") else 0
    v_codec = v_stream.get("codec_name", "unknown") if v_stream else "none"
    a_codec = a_stream.get("codec_name", "none") if a_stream else "none"
    sample_rate = a_stream.get("sample_rate", "") if a_stream else ""
    sample_rate_disp = f"{round(int(sample_rate)/1000, 1)}kHz" if sample_rate.isdigit() else (sample_rate or "44.1kHz")
    pix_fmt = v_stream.get("pix_fmt", "") if v_stream else ""
    fps_raw = v_stream.get("r_frame_rate", "") if v_stream else ""
    fps_disp, fps_num = format_fps(fps_raw)
    try:
        raw_bf = v_stream.get("has_b_frames", 2) if v_stream else 2
        has_b_frames = int(raw_bf) if raw_bf is not None else 2
    except Exception:
        has_b_frames = 2

    resolution = f"{width}x{height}" if width and height else "Unknown"

    v_time_base = (v_stream or {}).get("time_base", "") or ""
    a_time_base = (a_stream or {}).get("time_base", "") or ""
    nb_video_frames = 0
    try:
        raw_nb = (v_stream or {}).get("nb_frames")
        nb_video_frames = int(raw_nb) if raw_nb not in (None, "", "N/A") else 0
    except Exception:
        nb_video_frames = 0

    size_bytes = os.path.getsize(video_path)
    size_gb = size_bytes / (1024 * 1024 * 1024)
    size_text = f"{size_gb:.2f} GB" if size_gb >= 1.0 else f"{size_bytes / (1024 * 1024):.2f} MB"

    return {
        "path": video_path,
        "filename": os.path.basename(video_path),
        "duration": duration,
        "duration_formatted": format_seconds(duration),
        "resolution": resolution,
        "width": width,
        "height": height,
        "v_codec": v_codec,
        "a_codec": a_codec,
        "sample_rate": sample_rate,
        "sample_rate_formatted": sample_rate_disp,
        "pix_fmt": pix_fmt,
        "has_b_frames": has_b_frames,
        "fps": fps_disp,
        "fps_num": fps_num,
        "start_time": start_time,
        "v_time_base": v_time_base,
        "a_time_base": a_time_base,
        "nb_video_frames": nb_video_frames,
        "has_warning": False,
        "warning_detail": "",
        "size_text": size_text
    }

def analyze_merge_videos(video_infos: list[dict]):
    """Analyze video metadata to find master baseline and mismatched episodes."""
    if not video_infos:
        return {}, [], {}
    
    # 1. Resolutions: find majority
    res_counts = {}
    for v in video_infos:
        w, h = v.get("width", 0), v.get("height", 0)
        if w > 0 and h > 0:
            res_counts[(w, h)] = res_counts.get((w, h), 0) + 1
    master_w, master_h = max(res_counts.items(), key=lambda x: x[1])[0] if res_counts else (1280, 720)
    master_w = master_w if master_w % 2 == 0 else master_w + 1
    master_h = master_h if master_h % 2 == 0 else master_h + 1

    # 2. FPS: find majority
    fps_counts = {}
    for v in video_infos:
        f = v.get("fps_num", 0.0)
        if f > 0:
            f_round = round(f, 2)
            fps_counts[f_round] = fps_counts.get(f_round, 0) + 1
    master_fps_num = max(fps_counts.items(), key=lambda x: x[1])[0] if fps_counts else 24.0

    # Determine master FPS filter representation for FFmpeg
    if abs(master_fps_num - 23.976) < 0.01 or abs(master_fps_num - 23.98) < 0.01:
        master_fps_filter = "24000/1001"
    elif abs(master_fps_num - 29.97) < 0.01:
        master_fps_filter = "30000/1001"
    elif abs(master_fps_num - 59.94) < 0.01:
        master_fps_filter = "60000/1001"
    elif abs(master_fps_num - round(master_fps_num)) < 0.01:
        master_fps_filter = str(int(round(master_fps_num)))
    else:
        master_fps_filter = str(round(master_fps_num, 2))

    # 3. Audio Sample Rate: find majority
    sr_counts = {}
    for v in video_infos:
        sr = v.get("sample_rate", "")
        if sr and sr.isdigit():
            sr_counts[sr] = sr_counts.get(sr, 0) + 1
    master_sr = max(sr_counts.items(), key=lambda x: x[1])[0] if sr_counts else "44100"

    # 4. Codecs
    v_codecs = [v.get("v_codec") for v in video_infos if v.get("v_codec") and v.get("v_codec") != "unknown"]
    master_vcodec = max(set(v_codecs), key=v_codecs.count) if v_codecs else "h264"
    a_codecs = [v.get("a_codec") for v in video_infos if v.get("a_codec") and v.get("a_codec") not in ["none", "unknown"]]
    master_acodec = max(set(a_codecs), key=a_codecs.count) if a_codecs else "aac"

    # 5. Pixel Format
    pix_fmts = [v.get("pix_fmt") for v in video_infos if v.get("pix_fmt")]
    master_pix_fmt = max(set(pix_fmts), key=pix_fmts.count) if pix_fmts else "yuv420p"

    # 6. B-frames
    b_counts = [v.get("has_b_frames", 2) for v in video_infos]
    master_has_b_frames = max(set(b_counts), key=b_counts.count) if b_counts else 2

    # 7. Timeline compatibility (critical for lossless stream-copy concat):
    #    episodes produced by different tools often have a different start_time or timebase,
    #    which makes FFmpeg fail with "Non-monotonic DTS" when copying streams.
    st_counts = {}
    for v in video_infos:
        try:
            st_key = round(float(v.get("start_time") or 0.0), 1)
        except Exception:
            st_key = 0.0
        st_counts[st_key] = st_counts.get(st_key, 0) + 1
    master_start_time = max(st_counts.items(), key=lambda x: x[1])[0] if st_counts else 0.0

    vtb_counts = {}
    for v in video_infos:
        tb = v.get("v_time_base") or ""
        if tb:
            vtb_counts[tb] = vtb_counts.get(tb, 0) + 1
    master_v_time_base = max(vtb_counts.items(), key=lambda x: x[1])[0] if vtb_counts else ""

    mismatched_indices = []
    reasons_by_idx = {}

    for idx, v in enumerate(video_infos):
        w, h = v.get("width", 0), v.get("height", 0)
        f = v.get("fps_num", 0.0)
        sr = v.get("sample_rate", "")
        vc = v.get("v_codec", "")
        ac = v.get("a_codec", "")
        pfmt = v.get("pix_fmt", "")

        warns = []
        if (w > 0 and h > 0) and (w != master_w or h != master_h):
            warns.append(f"ទំហំ {w}x{h} (vs Master {master_w}x{master_h})")
        if f > 0 and abs(f - master_fps_num) >= 0.1:
            warns.append(f"FPS {v.get('fps')} (vs Master {master_fps_num:.2f} fps)")
        if sr and sr != master_sr:
            warns.append(f"Audio {sr}Hz (vs Master {master_sr}Hz)")
        if vc not in ["h264", "unknown", "none"] and vc != master_vcodec:
            warns.append(f"Codec {vc}")
        if ac not in ["aac", "unknown", "none"] and ac != master_acodec:
            warns.append(f"Audio {ac}")
        if pfmt and pfmt != master_pix_fmt:
            warns.append(f"Pixel format {pfmt} (vs Master {master_pix_fmt})")

        try:
            st = float(v.get("start_time") or 0.0)
        except Exception:
            st = 0.0
        if abs(st - master_start_time) > 0.5:
            warns.append(f"ចំណុចចាប់ផ្ដើម {st:.2f}s (vs Master {master_start_time:.2f}s)")

        vtb = v.get("v_time_base") or ""
        if vtb and master_v_time_base and vtb != master_v_time_base:
            warns.append(f"Timebase {vtb} (vs Master {master_v_time_base})")

        if warns:
            mismatched_indices.append(idx)
            reasons_by_idx[idx] = ", ".join(warns)

    baseline = {
        "master_w": master_w,
        "master_h": master_h,
        "master_fps_num": master_fps_num,
        "master_fps_filter": master_fps_filter,
        "master_sr": master_sr,
        "master_vcodec": master_vcodec,
        "master_acodec": master_acodec,
        "master_pix_fmt": master_pix_fmt,
        "master_has_b_frames": master_has_b_frames,
        "master_start_time": master_start_time,
        "master_v_time_base": master_v_time_base,
        "reasons_by_idx": reasons_by_idx
    }
    return baseline, mismatched_indices, reasons_by_idx

def compute_merge_partitions(
    video_infos: list[dict],
    partition_mode: str = "fps",
    episodes_per_part: int = 20,
    target_part_count: int = 3,
    custom_ranges: str = "",
    base_filename: str = "Merged_Video"
) -> list[dict]:
    """Compute video index slices and metadata for multi-part merging."""
    n = len(video_infos)
    if n == 0:
        return []

    root_fn, ext = os.path.splitext(base_filename)
    if not ext:
        ext = ".mp4"

    slices = []

    if partition_mode == "fps":
        # Group contiguous videos that have the same FPS (tolerance 0.1)
        cur_start = 0
        cur_fps = float(video_infos[0].get("fps_num") or 24.0)
        for i in range(1, n):
            v_fps = float(video_infos[i].get("fps_num") or 24.0)
            if abs(v_fps - cur_fps) >= 0.1:
                slices.append((cur_start, i))
                cur_start = i
                cur_fps = v_fps
        slices.append((cur_start, n))

    elif partition_mode == "count":
        step = max(1, episodes_per_part or 20)
        for i in range(0, n, step):
            slices.append((i, min(n, i + step)))

    elif partition_mode == "parts":
        target = max(1, min(n, target_part_count or 3))
        avg = n / target
        last = 0.0
        for i in range(target):
            nxt = last + avg
            s = int(round(last))
            e = int(round(nxt)) if i < target - 1 else n
            if e > s:
                slices.append((s, e))
            last = nxt

    elif partition_mode == "custom":
        raw_parts = [p.strip() for p in (custom_ranges or "").split(",") if p.strip()]
        for p in raw_parts:
            if "-" in p:
                try:
                    s_str, e_str = p.split("-", 1)
                    s_idx = max(0, int(s_str.strip()) - 1)
                    e_idx = min(n, int(e_str.strip()))
                    if e_idx > s_idx:
                        slices.append((s_idx, e_idx))
                except Exception:
                    pass
            else:
                try:
                    idx = int(p.strip()) - 1
                    if 0 <= idx < n:
                        slices.append((idx, idx + 1))
                except Exception:
                    pass
        if not slices:
            slices.append((0, n))
    else:
        slices.append((0, n))

    partitions = []
    for idx, (s, e) in enumerate(slices, start=1):
        sub_infos = video_infos[s:e]
        sub_dur = sum(v.get("duration", 0.0) for v in sub_infos)
        fps_vals = []
        for v in sub_infos:
            fps_str = v.get("fps")
            if fps_str and fps_str not in fps_vals:
                fps_vals.append(fps_str)
        fps_summary = ", ".join(fps_vals) if fps_vals else "N/A"
        
        _, mismatches, _ = analyze_merge_videos(sub_infos)
        is_lossless = len(mismatches) == 0

        part_fn = f"{root_fn}_Part_{idx}{ext}"
        partitions.append({
            "part_num": idx,
            "start_idx": s,
            "end_idx": e,
            "episode_count": e - s,
            "range_text": f"ភាគ {s + 1} ដល់ {e}",
            "range_tag": f"Ep {s + 1}-{e}",
            "duration": sub_dur,
            "duration_formatted": format_seconds(sub_dur),
            "fps_summary": fps_summary,
            "is_lossless_ready": is_lossless,
            "output_filename": part_fn
        })

    return partitions

@app.post("/api/preview-merge-partitions")
def preview_merge_partitions(req: PartitionPreviewRequest):
    if not req.videoPaths:
        return {"success": True, "partitions": [], "total_parts": 0, "total_videos": 0}

    video_infos = []
    for p in req.videoPaths:
        path_clean = p.strip().strip('"\'')
        if os.path.exists(path_clean):
            try:
                v_inf = probe_single_video(path_clean)
                video_infos.append(v_inf)
            except Exception:
                pass

    if not video_infos:
        return {"success": False, "message": "មិនអាចអានព័ត៌មានវីដេអូបានទេ។", "partitions": []}

    partitions = compute_merge_partitions(
        video_infos=video_infos,
        partition_mode=req.partitionMode or "fps",
        episodes_per_part=req.episodesPerPart or 20,
        target_part_count=req.targetPartCount or 3,
        custom_ranges=req.customRanges or "",
        base_filename=req.outputFilename or "Merged_Video"
    )

    return {
        "success": True,
        "partitions": partitions,
        "total_parts": len(partitions),
        "total_videos": len(video_infos)
    }

@app.post("/api/check-merge-compatibility")
def check_merge_compatibility(req: MergeCheckRequest):
    if not req.videoPaths:
        raise HTTPException(status_code=400, detail="សូមជ្រើសរើសវីដេអូយ៉ាងហោចណាស់ ២ ដើម្បីភ្ជាប់។")
    for p in req.videoPaths:
        register_media_dir(p)
    
    video_infos = []
    total_duration = 0.0

    for path in req.videoPaths:
        p = path.strip().strip('"\'')
        if not os.path.exists(p):
            raise HTTPException(status_code=404, detail=f"មិនរកឃើញឯកសារ៖ {os.path.basename(p)}")
        try:
            info = probe_single_video(p)
            video_infos.append(info)
            total_duration += info["duration"]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"មិនអាចអានព័ត៌មានវីដេអូ ({os.path.basename(p)}): {str(e)}")

    if len(video_infos) < 2:
        return {
            "success": True,
            "videos": video_infos,
            "total_duration": total_duration,
            "total_duration_formatted": format_seconds(total_duration),
            "is_compatible": True,
            "recommended_mode": "lossless",
            "message": "សូមជ្រើសរើសវីដេអូបន្ថែមដើម្បីភ្ជាប់គ្នា។"
        }

    baseline, mismatched_indices, reasons_by_idx = analyze_merge_videos(video_infos)
    is_compatible = len(mismatched_indices) == 0

    for idx, v in enumerate(video_infos):
        if idx in mismatched_indices:
            v["has_warning"] = True
            v["warning_detail"] = reasons_by_idx.get(idx, "")
        else:
            v["has_warning"] = False
            v["warning_detail"] = ""

    # Print comprehensive Episode Inspection Report to Terminal
    log_terminal("=" * 75)
    log_terminal(f"[Splitify] 📋 EPISODE INSPECTION REPORT ({len(video_infos)} episodes, Total: {format_seconds(total_duration)})")
    log_terminal(f"[Splitify] 🎯 Master Standard: {baseline['master_w']}x{baseline['master_h']} | {baseline['master_fps_num']:.2f} fps | {baseline['master_sr']}Hz")
    log_terminal("=" * 75)
    for idx, v in enumerate(video_infos, start=1):
        fps_str = v.get("fps", "N/A")
        sr_str = v.get("sample_rate_formatted", "") or "44.1kHz"
        warn_mark = f"⚠️ [{v['warning_detail']}]" if v.get("has_warning") else "✅ Master Match"
        fn_short = (v['filename'][:28] + '..') if len(v['filename']) > 30 else v['filename']
        log_terminal(f"  [{idx:2d}/{len(video_infos)}] {fn_short:<30} | {v['resolution']:<9} | {fps_str:<10} | {sr_str:<7} | {v['duration_formatted']} | {warn_mark}")

    if mismatched_indices:
        log_terminal("-" * 75)
        log_terminal(f"[Splitify] ⚠️ DETECTED {len(mismatched_indices)} EPISODE(S) WITH DISCREPANCIES (e.g. FPS/Resolution variance):")
        for orig_idx in mismatched_indices:
            ep_v = video_infos[orig_idx]
            log_terminal(f"   👉 Ep #{orig_idx+1} ({ep_v['filename']}): {reasons_by_idx.get(orig_idx, '')}")
        log_terminal(f"[Splitify] ⚡ SOLUTION: 'Selective Auto-Fix' will automatically normalize only these {len(mismatched_indices)} episodes (~1-2 mins),")
        log_terminal(f"   and Lossless Copy the other {len(video_infos)-len(mismatched_indices)} episodes for 100% video/voice synchronization!")
    else:
        log_terminal("-" * 75)
        log_terminal("[Splitify] ✅ ALL EPISODES ARE UNIFORM (Same resolution, same frame rate, same audio). Perfect for Lossless!")
    log_terminal("=" * 75)

    if is_compatible:
        recommended_mode = "lossless"
        message = "វីដេអូទាំងអស់មានទ្រង់ទ្រាយដូចគ្នាបេះបិទ អាចភ្ជាប់គ្នាដោយ Lossless Stream Copy (លឿនបំផុត ~១៥ វិនាទី & គុណភាពដើម ១០០%)!"
    else:
        recommended_mode = "auto"
        message = f"រកឃើញភាគចំនួន {len(mismatched_indices)} មាន FPS ឬទំហំខុសគេ។ ប្រព័ន្ធនឹងប្រើ Selective Auto-Fix កែតែភាគទាំងនោះ ហើយភ្ជាប់ Lossless ត្រឹមតែ ~១-២ នាទី (សំឡេង និងរូបភាព Sync ១០០%)!"

    return {
        "success": True,
        "videos": video_infos,
        "total_duration": total_duration,
        "total_duration_formatted": format_seconds(total_duration),
        "is_compatible": is_compatible,
        "recommended_mode": recommended_mode,
        "message": message
    }

# Background video splitting execution
def execute_split(req: SplitRequest):
    global split_state
    
    video_path = req.videoPath.strip()
    if (video_path.startswith('"') and video_path.endswith('"')) or (video_path.startswith("'") and video_path.endswith("'")):
        video_path = video_path[1:-1]
        
    output_folder = req.outputFolder.strip()
    os.makedirs(output_folder, exist_ok=True)
    register_media_dir(output_folder)
    
    ext = os.path.splitext(video_path)[1]
    if not ext:
        ext = ".mp4"

    total_parts = len(req.parts)
    output_files = []

    log_terminal(f"=== STARTING VIDEO SPLIT ({total_parts} parts) ===")
    log_terminal(f"Source: {os.path.basename(video_path)}")
    log_terminal(f"Output Directory: {output_folder}")

    with state_lock:
        # A cancel may arrive before this background task actually starts
        if split_state.get("status") == "canceled":
            log_terminal("[Split] Task was canceled before it started - aborting.")
            return
        split_state["is_running"] = True
        split_state["progress_percent"] = 0
        split_state["current_part"] = 0
        split_state["total_parts"] = total_parts
        split_state["status"] = "processing"
        split_state["task_type"] = "split"
        split_state["error_message"] = ""
        split_state["output_files"] = []
        split_state["output_folder"] = output_folder
        split_state["start_time"] = time.time()
        split_state["elapsed_time"] = 0.0

    try:
        for i, part in enumerate(req.parts):
            # Check for cancellation before starting next part
            with state_lock:
                if split_state.get("status") == "canceled":
                    log_terminal("[Split] Process canceled by user.")
                    break

            part_num = part.partNum
            start = part.start
            end = part.end
            
            # Format filename safely: Part_1_កុលាបក្រហម.mp4
            part_filename = f"Part_{part_num}_{req.outputPrefix}{ext}"
            part_output_path = os.path.join(output_folder, part_filename)

            log_terminal(f"[Split] [{part_num}/{total_parts}] Cutting: {part_filename} ({format_seconds(start)} -> {format_seconds(end)})...")

            # Update current status
            with state_lock:
                split_state["current_part"] = part_num
                split_state["elapsed_time"] = time.time() - split_state["start_time"]

            # ffmpeg command
            # Using -ss and -to for exact boundaries, and -c copy for speed (lossless)
            cmd = [
                get_ffmpeg_cmd(), "-y",
                "-ss", f"{start:.3f}",
                "-to", f"{end:.3f}",
                "-i", video_path,
                "-c", "copy",
                part_output_path
            ]

            # Create a log file for this part's ffmpeg output to prevent blocking the pipe
            log_file_path = os.path.join(output_folder, f"ffmpeg_part_{part_num}.log")
            
            try:
                with open(log_file_path, "wb") as log_file:
                    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
                    
                    with state_lock:
                        split_state["active_process"] = process

                    # Wait for completion
                    process.wait()

                # Check if execution was successful
                if process.returncode != 0 and split_state.get("status") != "canceled":
                    error_msg = "Unknown FFmpeg error"
                    try:
                        with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                            error_msg = f.read().strip()
                    except Exception:
                        pass
                    raise Exception(f"FFmpeg error on Part {part_num}: {error_msg}")

                # Clean up the log file on success
                try:
                    os.remove(log_file_path)
                except Exception:
                    pass
            except Exception as e:
                # Cleanup log file if error occurred
                try:
                    if os.path.exists(log_file_path):
                        os.remove(log_file_path)
                except Exception:
                    pass
                raise e

            output_files.append({
                "partNum": part_num,
                "filename": part_filename,
                "duration_formatted": format_seconds(part.duration)
            })

            # Calculate new progress percent
            percent = int((part_num / total_parts) * 100)
            elapsed_sec = int(time.time() - split_state["start_time"])
            log_terminal(f"[Split] [{part_num}/{total_parts}] ({percent}%) Done {part_filename} | Elapsed: {elapsed_sec}s")

            with state_lock:
                split_state["progress_percent"] = percent
                split_state["output_files"] = list(output_files)
                split_state["elapsed_time"] = time.time() - split_state["start_time"]

        # If we exited loop successfully without cancel
        with state_lock:
            if split_state.get("status") == "processing":
                split_state["status"] = "completed"
                split_state["is_running"] = False
                split_state["progress_percent"] = 100
                split_state["active_process"] = None

        total_elapsed = int(time.time() - split_state["start_time"])
        log_terminal(f"[Split] === SPLIT COMPLETED: Generated {len(output_files)} files in {total_elapsed}s ===")

    except Exception as e:
        hint = explain_ffmpeg_error(str(e))
        log_terminal(f"[Split] ERROR: {hint or str(e)}")
        with state_lock:
            split_state["status"] = "error"
            split_state["is_running"] = False
            split_state["error_message"] = hint or str(e)
            split_state["active_process"] = None
            
@app.post("/api/split")
def start_split(req: SplitRequest, background_tasks: BackgroundTasks):
    global split_state
    
    with state_lock:
        if split_state["is_running"]:
            raise HTTPException(status_code=400, detail="មានការបំបែកវីដេអូកំពុងដំណើរការរួចហើយ។")
        
        # Reset state synchronously to avoid race conditions with /api/progress
        split_state["is_running"] = True
        split_state["progress_percent"] = 0
        split_state["current_part"] = 0
        split_state["total_parts"] = len(req.parts)
        split_state["status"] = "processing"
        split_state["task_type"] = "split"
        split_state["error_message"] = ""
        split_state["output_files"] = []
        split_state["output_folder"] = req.outputFolder.strip()
        split_state["start_time"] = time.time()
        split_state["elapsed_time"] = 0.0
            
    background_tasks.add_task(execute_split, req)
    return {"success": True, "message": "ការបំបែកវីដេអូចាប់ផ្ដើមក្នុង Background"}

# Background video merging execution
def execute_merge(req: MergeRequest):
    global split_state

    video_paths = [p.strip().strip('"\'') for p in req.videoPaths if p.strip()]
    if len(video_paths) < 2:
        with state_lock:
            split_state["status"] = "error"
            split_state["is_running"] = False
            split_state["error_message"] = "ត្រូវការវីដេអូយ៉ាងហោចណាស់ ២ ដើម្បីភ្ជាប់។"
        return

    try:
        output_folder = req.outputFolder.strip()
        os.makedirs(output_folder, exist_ok=True)
        register_media_dir(output_folder)

        output_filename = req.outputFilename.strip()
        if not output_filename:
            output_filename = "Merged_Video"
        if not output_filename.endswith(".mp4") and not output_filename.endswith(".mkv"):
            output_filename += ".mp4"

        final_output_path = os.path.join(output_folder, output_filename)

        video_infos = []
        total_duration = 0.0
        for p in video_paths:
            try:
                v_inf = probe_single_video(p)
                video_infos.append(v_inf)
                total_duration += v_inf["duration"]
            except Exception:
                pass

        log_terminal(f"=== STARTING VIDEO MERGE ({len(video_paths)} videos) ===")
        log_terminal(f"Total Duration: {format_seconds(total_duration)} | Output: {output_filename}")

        with state_lock:
            # A cancel may arrive before this background task actually starts
            if split_state.get("status") == "canceled":
                log_terminal("[Merge] Task was canceled before it started - aborting.")
                return
            split_state["is_running"] = True
            split_state["progress_percent"] = 5
            split_state["current_part"] = 1
            split_state["total_parts"] = len(video_paths)
            split_state["status"] = "processing"
            split_state["task_type"] = "merge"
            split_state["error_message"] = ""
            split_state["output_files"] = []
            split_state["output_folder"] = output_folder
            split_state["start_time"] = time.time()
            split_state["elapsed_time"] = 0.0

        if req.enableMultiPart:
            try:
                execute_multi_part_merge(
                    req=req,
                    video_paths=video_paths,
                    video_infos=video_infos,
                    total_duration=total_duration,
                    output_folder=output_folder,
                    base_output_filename=output_filename
                )
            except Exception as e:
                hint = explain_ffmpeg_error(str(e))
                log_terminal(f"[Merge] ERROR: {hint or str(e)}")
                with state_lock:
                    if split_state.get("status") != "canceled":
                        split_state["status"] = "error"
                    split_state["is_running"] = False
                    split_state["error_message"] = hint or str(e)
                    split_state["active_process"] = None
            return

        mode = req.mode or "auto"
        temp_dir = get_temp_dir()
        concat_list_path = os.path.join(temp_dir, f"concat_list_{int(time.time()*1000)}.txt")

        try:
            # Build concat list file (safe for 10,000+ files)
            with open(concat_list_path, "w", encoding="utf-8") as f:
                for p in video_paths:
                    norm_p = os.path.abspath(p).replace("\\", "/")
                    escaped_p = norm_p.replace("'", "'\\''")
                    f.write(f"file '{escaped_p}'\n")

            baseline, mismatched_indices, reasons_by_idx = analyze_merge_videos(video_infos)
            log_file_path = os.path.join(output_folder, "ffmpeg_merge.log")

            if mode == "lossless":
                log_terminal("[Merge] Mode: LOSSLESS STREAM COPY (Forced by user)")
                perform_lossless_merge(
                    video_paths=video_paths,
                    concat_list_path=concat_list_path,
                    out_path=final_output_path,
                    duration=total_duration,
                    log_path=log_file_path,
                    temp_dir=temp_dir,
                    tag="forced_lossless",
                    reason="Forced by user",
                )
            elif mode == "reencode":
                log_terminal("[Merge] Mode: FULL RE-ENCODE (Forced by user)")
                target_w = baseline.get("master_w", 1280)
                target_h = baseline.get("master_h", 720)
                log_terminal(f"[Merge] Target Resolution: {target_w}x{target_h}")
                execute_batch_chunking_merge(video_paths, output_folder, final_output_path, target_w, target_h, total_duration)
            else:
                # "auto" or "selective"
                if len(mismatched_indices) == 0:
                    log_terminal("[Merge] Mode: 100% UNIFORM VIDEOS -> Lossless Stream Copy (auto-recovery enabled)")
                    try:
                        perform_lossless_merge(
                            video_paths=video_paths,
                            concat_list_path=concat_list_path,
                            out_path=final_output_path,
                            duration=total_duration,
                            log_path=log_file_path,
                            temp_dir=temp_dir,
                            tag="auto_uniform",
                            reason="all episodes look uniform",
                        )
                    except Exception as lossless_err:
                        if split_state.get("status") == "canceled":
                            raise
                        log_terminal(f"[Merge] ⚠️ Lossless merge failed ({lossless_err})")
                        log_terminal("[Merge] ↩ Falling back to Selective Auto-Fix for this batch...")
                        mismatched_indices = list(range(len(video_paths)))

                if mismatched_indices and len(mismatched_indices) <= int(len(video_paths) * 0.75):
                    log_terminal(f"[Merge] Mode: SELECTIVE AUTO-FIX ({len(mismatched_indices)}/{len(video_paths)} videos mismatched)")
                    execute_selective_autofix_merge(
                        video_paths=video_paths,
                        video_infos=video_infos,
                        mismatched_indices=mismatched_indices,
                        master_w=baseline["master_w"],
                        master_h=baseline["master_h"],
                        master_fps_filter=baseline["master_fps_filter"],
                        master_fps_num=baseline["master_fps_num"],
                        master_sr=baseline["master_sr"],
                        master_pix_fmt=baseline.get("master_pix_fmt", "yuv420p"),
                        master_has_b_frames=baseline.get("master_has_b_frames", 2),
                        output_folder=output_folder,
                        final_output_path=final_output_path,
                        total_duration=total_duration
                    )
                else:
                    log_terminal(f"[Merge] Mode: MAJORITY MISMATCHED ({len(mismatched_indices)}/{len(video_paths)}) -> Full Re-encode")
                    target_w = baseline.get("master_w", 1280)
                    target_h = baseline.get("master_h", 720)
                    log_terminal(f"[Merge] Target Resolution: {target_w}x{target_h}")
                    execute_batch_chunking_merge(video_paths, output_folder, final_output_path, target_w, target_h, total_duration)

            # Cleanup temp concat list and log file
            for fpath in [log_file_path, concat_list_path]:
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

            with state_lock:
                if split_state.get("status") == "processing":
                    split_state["status"] = "completed"
                    split_state["is_running"] = False
                    split_state["progress_percent"] = 100
                    split_state["active_process"] = None
                    split_state["output_files"] = [{
                        "partNum": 1,
                        "filename": output_filename,
                        "duration_formatted": format_seconds(total_duration),
                        "filepath": final_output_path
                    }]

            total_elapsed = int(time.time() - split_state["start_time"])
            log_terminal(f"[Merge] === MERGE COMPLETED in {total_elapsed}s! Saved to: {final_output_path} ===")

        except Exception as e:
            hint = explain_ffmpeg_error(str(e))
            log_terminal(f"[Merge] ERROR: {hint or str(e)}")
            for fpath in [concat_list_path]:
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
            with state_lock:
                if split_state.get("status") != "canceled":
                    split_state["status"] = "error"
                split_state["is_running"] = False
                split_state["error_message"] = hint or str(e)
                split_state["active_process"] = None


    except Exception as e:
        # Safety net: setup failures (bad output folder, permissions, ...) must never
        # leave the job stuck on "processing" (the UI would hang and new jobs get HTTP 400).
        hint = explain_ffmpeg_error(str(e))
        log_terminal(f"[Merge] ERROR: {hint or str(e)}")
        with state_lock:
            if split_state.get("status") != "canceled":
                split_state["status"] = "error"
            split_state["is_running"] = False
            split_state["error_message"] = hint or str(e)
            split_state["active_process"] = None

def get_optimal_target_resolution(video_paths: list[str]) -> tuple[int, int]:
    """Determine the optimal target resolution by probing sample videos."""
    sample_paths = video_paths[:10] if len(video_paths) > 10 else video_paths
    res_counts = {}
    for p in sample_paths:
        try:
            info = probe_single_video(p)
            w, h = info.get("width", 0), info.get("height", 0)
            if w > 0 and h > 0:
                res_counts[(w, h)] = res_counts.get((w, h), 0) + 1
        except Exception:
            pass
    if res_counts:
        best_w, best_h = max(res_counts.items(), key=lambda x: x[1])[0]
    else:
        best_w, best_h = 1280, 720
    best_w = best_w if best_w % 2 == 0 else best_w + 1
    best_h = best_h if best_h % 2 == 0 else best_h + 1
    return best_w, best_h

def run_ffmpeg_merge_process(cmd: list[str], log_file_path: str, total_duration: float, is_lossless: bool, raise_on_error: bool = True) -> int:
    """Execute FFmpeg process and track progress with real-time terminal logging.

    Returns the FFmpeg return code. When raise_on_error is False the caller decides
    whether a non-zero return code is fatal (used by the lossless retry chain).
    """
    global split_state
    last_logged_pct = -1

    with open(log_file_path, "wb") as log_file:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
        with state_lock:
            split_state["active_process"] = process

        last_log_time = 0.0
        while process.poll() is None:
            time.sleep(0.5)
            now = time.time()
            with state_lock:
                if split_state.get("status") == "canceled":
                    log_terminal("[Merge] Process canceled by user.")
                    break
                split_state["elapsed_time"] = now - split_state["start_time"]
                
                # Parse log file for real-time progress percentage
                if is_lossless:
                    cur_p = split_state["progress_percent"]
                    if cur_p < 95:
                        split_state["progress_percent"] = min(95, cur_p + 15)
                    pct = split_state["progress_percent"]
                    if (now - last_log_time >= 3.0) or pct != last_logged_pct:
                        last_log_time = now
                        last_logged_pct = pct
                        log_terminal(f"[Merge:Lossless] Progress: {pct}% | Elapsed: {format_seconds(int(split_state['elapsed_time']))}")
                else:
                    try:
                        if os.path.exists(log_file_path):
                            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                                lines = lf.readlines()[-8:]
                                for line in reversed(lines):
                                    m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
                                    if m:
                                        cur_sec = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                        m_speed = re.search(r"speed=\s*([\d\.]+)x", line)
                                        speed_str = m_speed.group(1) if m_speed else "1.0"
                                        m_fps = re.search(r"fps=\s*(\d+)", line)
                                        fps_str = m_fps.group(1) if m_fps else ""

                                        if total_duration > 0:
                                            pct = min(98, max(5, int((cur_sec / total_duration) * 100)))
                                            elapsed = int(split_state['elapsed_time'])
                                            eta_sec = int((elapsed / max(1, cur_sec)) * (total_duration - cur_sec)) if cur_sec > 5 else 0
                                            
                                            with state_lock:
                                                split_state["progress_percent"] = pct
                                                split_state["eta_seconds"] = eta_sec
                                                split_state["speed"] = speed_str
                                                split_state["fps"] = fps_str

                                            if (now - last_log_time >= 3.0) or pct == 98:
                                                last_log_time = now
                                                last_logged_pct = pct
                                                fps_info = f" ({fps_str} fps)" if fps_str else ""
                                                log_terminal(f"[Merge:Re-encode] Video Time: {format_seconds(cur_sec)}/{format_seconds(total_duration)} | Speed: {speed_str}x{fps_info} | Progress: {pct}% | Elapsed: {format_seconds(elapsed)} | ETA: ~{format_seconds(eta_sec)}")
                                        break
                    except Exception:
                        pass

        process.wait()
        with state_lock:
            if split_state.get("active_process") is process:
                split_state["active_process"] = None

    if process.returncode != 0 and split_state.get("status") != "canceled":
        err_text = "Unknown FFmpeg error"
        try:
            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
                non_progress = [l for l in lines if not l.startswith("frame=")][-15:]
                err_text = "\n".join(non_progress) if non_progress else "\n".join(lines[-10:])
        except Exception:
            pass
        hint = explain_ffmpeg_error(err_text)
        if not raise_on_error:
            # Part of the lossless retry chain: this is NOT fatal, the next strategy is tried.
            log_lines = [l.strip() for l in err_text.splitlines() if l.strip()]
            keywords = ("Error", "error", "Invalid", "Non-monotonic", "failed", "Invalid data")
            error_line = next(
                (l for l in reversed(log_lines) if any(k in l for k in keywords)),
                log_lines[-1] if log_lines else "",
            )
            # strip the noisy "[stream @ 0000...]" prefix
            error_line = re.sub(r"\[[^\]]*@\s*[0-9a-fx]+\]\s*", "", error_line)
            log_terminal(f"[Merge:Retry] ⚠️ Strategy failed (exit {process.returncode}): {error_line[:160]}")
            log_terminal("[Merge:Retry]    ↪ Trying the next strategy automatically...")
        else:
            log_terminal("=" * 65)
            log_terminal(f"[Merge:ERROR] ❌ FFmpeg ERROR occurred during merge (Exit code: {process.returncode})!")
            log_terminal("Error details from FFmpeg:")
            for l in err_text.splitlines():
                log_terminal(f"  {l}")
            if hint:
                log_terminal(f"[Merge:ERROR] 💡 {hint}")
            log_terminal("=" * 65)
            raise Exception(f"{hint or 'FFmpeg error'}\n--- FFmpeg output ---\n{err_text}")

    return process.returncode

def _concat_list_file(video_paths: list[str], list_path: str) -> str:
    """Write a FFmpeg concat-demuxer list file and return its path."""
    with open(list_path, "w", encoding="utf-8") as f:
        for p in video_paths:
            norm_p = os.path.abspath(p).replace("\\", "/")
            escaped_p = norm_p.replace("'", "'\\''")
            f.write(f"file '{escaped_p}'\n")
    return list_path

def _lossless_concat_cmd(list_path: str, out_path: str, flags: tuple) -> list[str]:
    return [
        get_ffmpeg_cmd(), "-y",
        *flags,
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        out_path,
    ]

def remux_videos_to_mpegts(video_paths: list[str], temp_dir: str, tag: str) -> list[str]:
    """Losslessly remux every input into MPEG-TS so the concat timeline is always continuous."""
    ts_paths = []
    for idx, src in enumerate(video_paths):
        ts_path = os.path.join(temp_dir, f"ts_{tag}_{idx:05d}.ts")
        cmd = [
            get_ffmpeg_cmd(), "-y",
            "-fflags", "+genpts",
            "-i", src,
            "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-f", "mpegts",
            ts_path,
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise Exception(f"មិនអាចបម្លែងឯកសារទី {idx + 1} ({os.path.basename(src)}) ទៅ MPEG-TS បានឡើយ។")
        ts_paths.append(ts_path)
    return ts_paths

def perform_lossless_merge(
    video_paths: list[str],
    concat_list_path: str,
    out_path: str,
    duration: float,
    log_path: str,
    temp_dir: str,
    tag: str,
    reason: str = ""
) -> bool:
    """Try every lossless strategy in order; return True as soon as one produces a file.

    Chain: plain stream copy -> stream copy with timestamp repair -> MPEG-TS remux + concat.
    Raises the last FFmpeg error (with a friendly Khmer message) when nothing worked.
    """
    global split_state

    def _cleanup_partial():
        for bad in (out_path, out_path + ".ts"):
            if os.path.exists(bad):
                try:
                    os.remove(bad)
                except Exception:
                    pass

    def _is_canceled() -> bool:
        with state_lock:
            return split_state.get("status") == "canceled"

    def _read_log_tail(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as lf:
                return lf.read()[-4000:]
        except Exception:
            return ""

    attempts = [
        ("Attempt 1/3 – Lossless (stream copy)", tuple(LOSSLESS_BASE_FLAGS)),
        ("Attempt 2/3 – Lossless with timestamp repair", tuple(LOSSLESS_REPAIR_FLAGS)),
    ]

    last_error = ""
    for label, flags in attempts:
        if _is_canceled():
            raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")
        _cleanup_partial()
        log_terminal(f"[Merge:Retry] {label}{(' | ' + reason) if reason else ''}")
        cmd = _lossless_concat_cmd(concat_list_path, out_path, flags)
        code = run_ffmpeg_merge_process(cmd, log_path, duration, is_lossless=True, raise_on_error=False)
        if code == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log_terminal(f"[Merge:Retry] ✅ Lossless merge succeeded ({label.split('–')[-1].strip()}).")
            return True
        last_error = _read_log_tail(log_path) or "FFmpeg exited with a non-zero status"
        if _is_canceled():
            raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")

    # Attempt 3: MPEG-TS remux (very robust, still lossless / no re-encode)
    if _is_canceled():
        raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")
    _cleanup_partial()
    log_terminal("[Merge:Retry] Attempt 3/3 – Lossless via MPEG-TS remux (rebuilds a clean timeline)")
    ts_paths = remux_videos_to_mpegts(video_paths, temp_dir, tag)
    try:
        ts_list = _concat_list_file(ts_paths, os.path.join(temp_dir, f"ts_concat_{tag}.txt"))
        cmd = [
            get_ffmpeg_cmd(), "-y",
            "-fflags", "+genpts",
            "-f", "concat",
            "-safe", "0",
            "-i", ts_list,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-avoid_negative_ts", "make_zero",
            "-max_interleave_delta", "0",
            out_path,
        ]
        code = run_ffmpeg_merge_process(cmd, log_path, duration, is_lossless=True, raise_on_error=False)
        if code == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            log_terminal("[Merge:Retry] ✅ Lossless merge succeeded using the MPEG-TS strategy.")
            return True
        last_error = _read_log_tail(log_path) or last_error
    finally:
        for ts in ts_paths:
            if os.path.exists(ts):
                try:
                    os.remove(ts)
                except Exception:
                    pass

    _cleanup_partial()
    hint = explain_ffmpeg_error(last_error) or (
        "ការភ្ជាប់បែប Lossless បរាជ័យគ្រប់វិធី។ សូមជ្រើស Mode “Auto / Selective Auto-Fix” ឬ “Full Re-encode”។"
    )
    raise Exception(hint)

def execute_selective_autofix_merge(
    video_paths: list[str],
    video_infos: list[dict],
    mismatched_indices: list[int],
    master_w: int,
    master_h: int,
    master_fps_filter: str,
    master_fps_num: float,
    master_sr: str,
    output_folder: str,
    final_output_path: str,
    total_duration: float,
    master_pix_fmt: str = "yuv420p",
    master_has_b_frames: int = 2
):
    """Normalize ONLY the mismatched episodes to master specs, then perform lossless stream copy on all episodes."""
    global split_state
    temp_dir = get_temp_dir()
    num_to_fix = len(mismatched_indices)
    
    log_terminal("=" * 75)
    log_terminal(f"[Splitify:SmartFix] ⚡ SELECTIVE AUTO-FIX ACTIVATED: {num_to_fix}/{len(video_paths)} videos need normalization.")
    log_terminal(f"[Splitify:SmartFix] 🎯 Master Standard: {master_w}x{master_h} | {master_fps_num:.2f} fps | {master_sr}Hz | {master_pix_fmt} | BF:{master_has_b_frames}")
    log_terminal(f"[Splitify:SmartFix] Normalizing ONLY the {num_to_fix} mismatched episode(s)... ({len(video_paths) - num_to_fix} episodes will be copied lossless - 0s render!)")
    log_terminal("=" * 75)

    normalized_paths = list(video_paths)
    temp_files_created = []

    try:
        for fix_idx, orig_idx in enumerate(mismatched_indices):
            with state_lock:
                if split_state.get("status") == "canceled":
                    raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")
                split_state["current_part"] = fix_idx + 1
                split_state["total_parts"] = num_to_fix
                pct = int((fix_idx / num_to_fix) * 85)
                split_state["progress_percent"] = max(5, pct)

            orig_path = video_paths[orig_idx]
            v_info = video_infos[orig_idx] if orig_idx < len(video_infos) else {}
            ep_name = v_info.get("filename", os.path.basename(orig_path))
            
            temp_out = os.path.join(temp_dir, f"smartfix_{int(time.time()*1000)}_{orig_idx}.mp4")
            temp_files_created.append(temp_out)
            normalized_paths[orig_idx] = temp_out
            
            log_temp_path = os.path.join(temp_dir, f"smartfix_log_{orig_idx}.log")
            temp_files_created.append(log_temp_path)

            cur_fps = v_info.get('fps', 'N/A')
            log_terminal(f"[Splitify:SmartFix] [{fix_idx+1}/{num_to_fix}] Normalizing Ep #{orig_idx+1} ({ep_name}) [{cur_fps} -> {master_fps_num:.2f} fps]...")

            start_fix_t = time.time()
            has_audio = v_info.get("a_codec") not in ["none", "", None]
            b_preset = "veryfast" if master_has_b_frames > 0 else "ultrafast"
            b_val = str(master_has_b_frames)

            if has_audio:
                fix_cmd = [
                    get_ffmpeg_cmd(), "-y",
                    "-i", orig_path,
                    "-vf", f"scale={master_w}:{master_h}:force_original_aspect_ratio=decrease,pad={master_w}:{master_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={master_fps_filter},format={master_pix_fmt}",
                    "-af", f"aformat=sample_rates={master_sr}:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.100000:first_pts=0",
                    "-fps_mode", "cfr",
                    "-c:v", "libx264",
                    "-preset", b_preset,
                    "-bf", b_val,
                    "-crf", "18",
                    "-threads", "0",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", str(master_sr),
                    "-ac", "2",
                    temp_out
                ]
            else:
                fix_cmd = [
                    get_ffmpeg_cmd(), "-y",
                    "-i", orig_path,
                    "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={master_sr}",
                    "-vf", f"scale={master_w}:{master_h}:force_original_aspect_ratio=decrease,pad={master_w}:{master_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={master_fps_filter},format={master_pix_fmt}",
                    "-fps_mode", "cfr",
                    "-c:v", "libx264",
                    "-preset", b_preset,
                    "-bf", b_val,
                    "-crf", "18",
                    "-threads", "0",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", str(master_sr),
                    "-ac", "2",
                    "-shortest",
                    temp_out
                ]

            with open(log_temp_path, "wb") as log_file:
                proc = subprocess.Popen(fix_cmd, stdout=subprocess.DEVNULL, stderr=log_file)
                with state_lock:
                    split_state["active_process"] = proc

                last_log_t = 0.0
                while proc.poll() is None:
                    time.sleep(0.4)
                    now = time.time()
                    with state_lock:
                        if split_state.get("status") == "canceled":
                            log_terminal("[Splitify:SmartFix] Process canceled by user.")
                            break
                        split_state["elapsed_time"] = now - split_state["start_time"]

                    try:
                        if os.path.exists(log_temp_path):
                            with open(log_temp_path, "r", encoding="utf-8", errors="ignore") as lf:
                                lines = lf.readlines()[-6:]
                                for line in reversed(lines):
                                    m_spd = re.search(r"speed=\s*([\d\.]+)x", line)
                                    m_fps = re.search(r"fps=\s*(\d+)", line)
                                    m_time = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
                                    if m_time:
                                        cur_sec = int(m_time.group(1))*3600 + int(m_time.group(2))*60 + float(m_time.group(3))
                                        ep_dur = max(1.0, v_info.get("duration", 60.0))
                                        in_ep_pct = min(1.0, cur_sec / ep_dur)
                                        overall_smartfix_pct = min(88, max(5, int(((fix_idx + in_ep_pct) / num_to_fix) * 85)))
                                        with state_lock:
                                            split_state["progress_percent"] = overall_smartfix_pct
                                    if m_spd:
                                        with state_lock:
                                            split_state["speed"] = m_spd.group(1)
                                            if m_fps: split_state["fps"] = m_fps.group(1)
                                        if now - last_log_t >= 3.0:
                                            last_log_t = now
                                            fps_str = f" ({m_fps.group(1)} fps)" if m_fps else ""
                                            log_terminal(f"[Splitify:SmartFix] [{fix_idx+1}/{num_to_fix}] Ep #{orig_idx+1} | Speed: {m_spd.group(1)}x{fps_str} | Overall: {split_state['progress_percent']}%")
                                        break
                    except Exception:
                        pass

                proc.wait()

            if proc.returncode != 0 and split_state.get("status") != "canceled":
                err_text = ""
                try:
                    with open(log_temp_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [l.strip() for l in f.readlines() if l.strip()]
                        err_text = "\n".join(lines[-10:])
                except Exception:
                    pass
                log_terminal("=" * 65)
                log_terminal(f"[Splitify:SmartFix] ❌ FFmpeg failed on Ep #{orig_idx+1} ({ep_name})!")
                log_terminal(f"Error details: {err_text}")
                log_terminal("=" * 65)
                raise Exception(f"Smart-Fix normalization failed on Ep #{orig_idx+1}:\n{err_text}")

            dur_fix = time.time() - start_fix_t
            log_terminal(f"[Splitify:SmartFix] [{fix_idx+1}/{num_to_fix}] Ep #{orig_idx+1} ({ep_name}) normalized successfully in {dur_fix:.1f}s.")

        # All mismatched episodes normalized! Now Lossless Concat Stream Copy on all files
        log_terminal(f"[Splitify:SmartFix] All {num_to_fix} mismatched episode(s) normalized! Performing final Lossless Concat Stream Copy on all {len(video_paths)} episodes...")
        with state_lock:
            split_state["progress_percent"] = 90
            split_state["current_part"] = num_to_fix

        concat_list_path = _concat_list_file(normalized_paths, os.path.join(temp_dir, f"smartfix_concat_{int(time.time()*1000)}.txt"))
        temp_files_created.append(concat_list_path)
        log_final_path = os.path.join(temp_dir, f"smartfix_final_{int(time.time()*1000)}.log")
        temp_files_created.append(log_final_path)

        perform_lossless_merge(
            video_paths=normalized_paths,
            concat_list_path=concat_list_path,
            out_path=final_output_path,
            duration=total_duration,
            log_path=log_final_path,
            temp_dir=temp_dir,
            tag="smartfix",
            reason="after normalizing mismatched episodes",
        )

    finally:
        # Clean up temporary files
        for fpath in temp_files_created:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                except Exception:
                    pass

def execute_multi_part_merge(
    req: MergeRequest,
    video_paths: list[str],
    video_infos: list[dict],
    total_duration: float,
    output_folder: str,
    base_output_filename: str
):
    """Execute multi-part merge by partitioning videos and merging each part."""
    global split_state

    partitions = compute_merge_partitions(
        video_infos=video_infos,
        partition_mode=req.partitionMode or "fps",
        episodes_per_part=req.episodesPerPart or 20,
        target_part_count=req.targetPartCount or 3,
        custom_ranges=req.customRanges or "",
        base_filename=base_output_filename
    )

    if not partitions:
        raise Exception("មិនមានភាគវីដេអូសម្រាប់បែងចែកជា Part ឡើយ។")

    total_parts = len(partitions)
    log_terminal("=" * 75)
    log_terminal(f"[Splitify:MultiPart] ⚡ STARTING MULTI-PART MERGE: {total_parts} Part(s) to generate")
    log_terminal(f"[Splitify:MultiPart] Mode: {(req.partitionMode or 'fps').upper()} | Output Folder: {output_folder}")
    log_terminal("=" * 75)

    with state_lock:
        split_state["total_parts"] = total_parts
        split_state["current_part"] = 0
        split_state["progress_percent"] = 2
        split_state["output_files"] = []

    generated_files = []
    temp_dir = get_temp_dir()

    for p_idx, part in enumerate(partitions):
        with state_lock:
            if split_state.get("status") == "canceled":
                raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")
            split_state["current_part"] = p_idx + 1
            split_state["progress_percent"] = int((p_idx / total_parts) * 95)

        s, e = part["start_idx"], part["end_idx"]
        part_paths = video_paths[s:e]
        part_infos = video_infos[s:e]
        part_out_fn = part["output_filename"]
        part_out_path = os.path.join(output_folder, part_out_fn)
        part_dur = part["duration"]
        part_range = part["range_tag"]

        log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] Merging {part_range} ({len(part_paths)} episodes) -> {part_out_fn}...")

        # Concat list for this part
        part_concat_path = _concat_list_file(part_paths, os.path.join(temp_dir, f"part_concat_{int(time.time()*1000)}_{p_idx}.txt"))

        log_file_path = os.path.join(temp_dir, f"part_log_{int(time.time()*1000)}_{p_idx}.log")

        # Check uniformity within this partition
        part_baseline, part_mismatches, _ = analyze_merge_videos(part_infos)
        is_part_lossless = (len(part_mismatches) == 0)

        start_p_t = time.time()
        try:
            if is_part_lossless:
                log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] Uniform specs -> Lossless Stream Copy (auto-recovery enabled)...")
                try:
                    perform_lossless_merge(
                        video_paths=part_paths,
                        concat_list_path=part_concat_path,
                        out_path=part_out_path,
                        duration=part_dur,
                        log_path=log_file_path,
                        temp_dir=temp_dir,
                        tag=f"part{p_idx + 1}",
                        reason=part_range,
                    )
                except Exception as lossless_err:
                    if split_state.get("status") == "canceled":
                        raise
                    log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] ⚠️ Lossless failed ({lossless_err})")
                    log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] ↩ Falling back to Selective Auto-Fix...")
                    is_part_lossless = False

            if not is_part_lossless:
                log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] Mismatched specs detected ({len(part_mismatches)}/{len(part_paths)}) -> Selective Auto-Fix...")
                execute_selective_autofix_merge(
                    video_paths=part_paths,
                    video_infos=part_infos,
                    mismatched_indices=part_mismatches,
                    master_w=part_baseline["master_w"],
                    master_h=part_baseline["master_h"],
                    master_fps_filter=part_baseline["master_fps_filter"],
                    master_fps_num=part_baseline["master_fps_num"],
                    master_sr=part_baseline["master_sr"],
                    output_folder=output_folder,
                    final_output_path=part_out_path,
                    total_duration=part_dur,
                    master_pix_fmt=part_baseline.get("master_pix_fmt", "yuv420p"),
                    master_has_b_frames=part_baseline.get("master_has_b_frames", 2)
                )

            dur_taken = time.time() - start_p_t
            log_terminal(f"[Splitify:MultiPart] [{p_idx + 1}/{total_parts}] ✅ {part_out_fn} completed in {dur_taken:.1f}s ({format_seconds(part_dur)})")

            file_entry = {
                "partNum": p_idx + 1,
                "filename": part_out_fn,
                "range": part_range,
                "duration_formatted": format_seconds(part_dur),
                "filepath": part_out_path
            }
            generated_files.append(file_entry)
            with state_lock:
                split_state["output_files"] = list(generated_files)
                split_state["progress_percent"] = int(((p_idx + 1) / total_parts) * 100)

        except Exception:
            # Never leave a half-written part file behind for the user to trip over
            if os.path.exists(part_out_path):
                try:
                    os.remove(part_out_path)
                    log_terminal(f"[Splitify:MultiPart] Removed incomplete file: {part_out_fn}")
                except Exception:
                    pass
            raise
        finally:
            for fpath in [part_concat_path, log_file_path]:
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

    with state_lock:
        if split_state.get("status") == "processing":
            split_state["status"] = "completed"
            split_state["is_running"] = False
            split_state["progress_percent"] = 100
            split_state["active_process"] = None
            split_state["output_files"] = generated_files

    total_elapsed = int(time.time() - split_state["start_time"])
    log_terminal("=" * 75)
    log_terminal(f"[Splitify:MultiPart] 🎉 ALL {total_parts} PARTS GENERATED SUCCESSFULLY in {total_elapsed}s!")
    log_terminal(f"[Splitify:MultiPart] Saved {len(generated_files)} file(s) to: {output_folder}")
    log_terminal("=" * 75)

def execute_batch_chunking_merge(video_paths: list[str], output_folder: str, final_output_path: str, target_w: int, target_h: int, total_duration: float):
    """Fallback method: chunk videos into groups of 20, normalize each group, then concat chunks lossless."""
    global split_state
    temp_dir = get_temp_dir()
    CHUNK_SIZE = 20
    chunks = [video_paths[i:i + CHUNK_SIZE] for i in range(0, len(video_paths), CHUNK_SIZE)]
    total_chunks = len(chunks)
    chunk_output_files = []

    log_terminal(f"[Merge:Batch] Starting Batch Chunking: {len(video_paths)} videos -> {total_chunks} chunks (chunk size: {CHUNK_SIZE})")

    try:
        for chunk_idx, chunk_videos in enumerate(chunks):
            with state_lock:
                if split_state.get("status") == "canceled":
                    raise Exception("ការងារត្រូវបានបោះបង់ (Canceled)")
                split_state["current_part"] = chunk_idx + 1
                split_state["total_parts"] = total_chunks
                pct = int(((chunk_idx) / total_chunks) * 90)
                split_state["progress_percent"] = max(5, pct)

            log_terminal(f"[Merge:Batch] Processing Chunk [{chunk_idx+1}/{total_chunks}] ({len(chunk_videos)} videos)...")

            chunk_out = os.path.join(temp_dir, f"temp_chunk_{int(time.time()*1000)}_{chunk_idx}.mp4")
            chunk_output_files.append(chunk_out)
            log_chunk_path = os.path.join(temp_dir, f"log_chunk_{chunk_idx}.log")

            # Build small command for this chunk (only 20 inputs max)
            cmd = [get_ffmpeg_cmd(), "-y", "-fflags", "+genpts"]
            filter_chains = []
            concat_inputs = []

            for i, p in enumerate(chunk_videos):
                cmd.extend(["-i", p])
                filter_chains.append(
                    f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}];"
                    f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo,aresample=async=1000:min_hard_comp=0.100000:first_pts=0[a{i}]"
                )
                concat_inputs.append(f"[v{i}][a{i}]")

            concat_filter = f"{';'.join(filter_chains)};{''.join(concat_inputs)}concat=n={len(chunk_videos)}:v=1:a=1[outv][outa]"
            cmd.extend([
                "-filter_complex", concat_filter,
                "-map", "[outv]",
                "-map", "[outa]",
                "-fps_mode", "cfr",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-threads", "0",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-avoid_negative_ts", "make_zero",
                chunk_out
            ])

            with open(log_chunk_path, "wb") as log_file:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file)
                with state_lock:
                    split_state["active_process"] = proc

                last_log_time = 0.0
                while proc.poll() is None:
                    time.sleep(0.5)
                    now = time.time()
                    with state_lock:
                        if split_state.get("status") == "canceled":
                            log_terminal("[Merge:Batch] Process canceled by user.")
                            break
                        split_state["elapsed_time"] = now - split_state["start_time"]

                    try:
                        if os.path.exists(log_chunk_path):
                            with open(log_chunk_path, "r", encoding="utf-8", errors="ignore") as lf:
                                lines = lf.readlines()[-8:]
                                for line in reversed(lines):
                                    m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", line)
                                    if m:
                                        cur_sec = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                                        chunk_dur_est = max(1.0, total_duration / total_chunks)
                                        in_chunk_pct = min(1.0, cur_sec / chunk_dur_est)
                                        pct = min(96, max(5, int(((chunk_idx + in_chunk_pct) / total_chunks) * 95)))
                                        
                                        m_speed = re.search(r"speed=\s*([\d\.]+)x", line)
                                        speed_str = m_speed.group(1) if m_speed else "1.0"
                                        m_fps = re.search(r"fps=\s*(\d+)", line)
                                        fps_str = m_fps.group(1) if m_fps else ""

                                        elapsed = int(split_state['elapsed_time'])
                                        overall_video_sec = (chunk_idx * chunk_dur_est) + cur_sec
                                        eta_sec = int((elapsed / max(1, overall_video_sec)) * max(0, total_duration - overall_video_sec)) if overall_video_sec > 5 else 0

                                        with state_lock:
                                            split_state["progress_percent"] = pct
                                            split_state["eta_seconds"] = eta_sec
                                            split_state["speed"] = speed_str
                                            split_state["fps"] = fps_str

                                        if (now - last_log_time >= 3.0) or pct == 96:
                                            last_log_time = now
                                            fps_info = f" ({fps_str} fps)" if fps_str else ""
                                            log_terminal(
                                                f"[Merge:Re-encode] Chunk [{chunk_idx+1}/{total_chunks}] | "
                                                f"Video: {format_seconds(overall_video_sec)}/{format_seconds(total_duration)} | "
                                                f"Speed: {speed_str}x{fps_info} | "
                                                f"Progress: {pct}% | "
                                                f"Elapsed: {format_seconds(elapsed)} | "
                                                f"ETA: ~{format_seconds(eta_sec)}"
                                            )
                                        break
                    except Exception:
                        pass

                proc.wait()

            if proc.returncode != 0 and split_state.get("status") != "canceled":
                err_text = ""
                try:
                    with open(log_chunk_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [l.strip() for l in f.readlines() if l.strip()]
                        non_progress = [l for l in lines if not l.startswith("frame=")][-15:]
                        err_text = "\n".join(non_progress) if non_progress else "\n".join(lines[-10:])
                except Exception:
                    pass
                log_terminal("=" * 65)
                log_terminal(f"[Merge:ERROR] ❌ FFmpeg failed on Chunk [{chunk_idx+1}/{total_chunks}] (Exit code: {proc.returncode})!")
                log_terminal("Error details from FFmpeg:")
                for l in err_text.splitlines():
                    log_terminal(f"  {l}")
                log_terminal("=" * 65)
                raise Exception(f"FFmpeg error on chunk #{chunk_idx+1}:\n{err_text}")

            log_terminal(f"[Merge:Batch] Chunk [{chunk_idx+1}/{total_chunks}] successfully created.")

            if os.path.exists(log_chunk_path):
                try: os.remove(log_chunk_path)
                except Exception: pass

        # Now merge all chunk files losslessly
        log_terminal(f"[Merge:Batch] All {total_chunks} chunks ready. Performing final lossless stream copy merge...")
        chunks_list_path = _concat_list_file(chunk_output_files, os.path.join(temp_dir, f"chunks_list_{int(time.time()*1000)}.txt"))
        log_final_path = os.path.join(temp_dir, "log_final_merge.log")

        perform_lossless_merge(
            video_paths=chunk_output_files,
            concat_list_path=chunks_list_path,
            out_path=final_output_path,
            duration=total_duration,
            log_path=log_final_path,
            temp_dir=temp_dir,
            tag="batch_final",
            reason=f"{total_chunks} normalized chunks",
        )

        if os.path.exists(chunks_list_path):
            try: os.remove(chunks_list_path)
            except Exception: pass
        if os.path.exists(log_final_path):
            try: os.remove(log_final_path)
            except Exception: pass

    finally:
        # Cleanup temporary chunk files
        for cp in chunk_output_files:
            if os.path.exists(cp):
                try: os.remove(cp)
                except Exception: pass

@app.post("/api/merge")
def start_merge(req: MergeRequest, background_tasks: BackgroundTasks):
    global split_state
    
    with state_lock:
        if split_state["is_running"]:
            raise HTTPException(status_code=400, detail="មានការបំបែក ឬភ្ជាប់វីដេអូកំពុងដំណើរការរួចហើយ។")
        
        split_state["is_running"] = True
        split_state["progress_percent"] = 0
        split_state["current_part"] = 1
        split_state["total_parts"] = len(req.videoPaths)
        split_state["status"] = "processing"
        split_state["task_type"] = "merge"
        split_state["error_message"] = ""
        split_state["output_files"] = []
        split_state["output_folder"] = req.outputFolder.strip()
        split_state["start_time"] = time.time()
        split_state["elapsed_time"] = 0.0
            
    background_tasks.add_task(execute_merge, req)
    return {"success": True, "message": "ការភ្ជាប់វីដេអូចាប់ផ្ដើមក្នុង Background"}

@app.get("/api/progress")
def get_progress():
    def event_generator():
        while True:
            with state_lock:
                state_data = {
                    "is_running": split_state["is_running"],
                    "progress_percent": split_state["progress_percent"],
                    "current_part": split_state["current_part"],
                    "total_parts": split_state["total_parts"],
                    "status": split_state["status"],
                    "task_type": split_state.get("task_type", "split"),
                    "error_message": split_state["error_message"],
                    "output_files": split_state["output_files"],
                    "output_folder": split_state["output_folder"],
                    "elapsed_time": int(split_state["elapsed_time"]),
                    "eta_seconds": split_state.get("eta_seconds", 0),
                    "speed": split_state.get("speed", "1.0"),
                    "fps": split_state.get("fps", "")
                }
            
            yield f"data: {json.dumps(state_data)}\n\n"
            
            # Stop stream if finished or idle
            if state_data["status"] in ["completed", "canceled", "error", "idle"]:
                break
                
            time.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/api/cancel")
def cancel_split():
    global split_state
    log_terminal("Task cancellation requested by user.")
    with state_lock:
        if not split_state["is_running"] and split_state.get("status") != "processing":
            return {"success": True, "message": "មិនមានការងារកំពុងរត់ឡើយ"}
            
        split_state["status"] = "canceled"
        split_state["is_running"] = False
        
        # Kill the active subprocess
        if split_state["active_process"]:
            try:
                split_state["active_process"].kill()
            except Exception:
                pass
            split_state["active_process"] = None
            
    return {"success": True, "message": "បានបោះបង់ការងារជោគជ័យ"}

@app.post("/api/open-folder")
def open_folder(req: Optional[OpenFolderRequest] = None):
    global split_state
    folder = ""
    if req and req.folder:
        folder = req.folder.strip()
    if not folder:
        folder = split_state.get("output_folder", "")
    if not folder or not os.path.isdir(folder):
        # Fallback to the app's own output folder
        folder = get_data_dir()

    folder = os.path.abspath(folder)
    # Only real directories may be opened (never a file/executable path)
    if not os.path.isdir(folder):
        raise HTTPException(status_code=400, detail="ផ្លូវនេះមិនមែនជា Folder ឡើយ។")

    log_terminal(f"Opened output folder in Explorer: {folder}")

    try:
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"មិនអាចបើក Folder បានទេ៖ {str(e)}")

@app.get("/api/play-video")
def play_video(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_VIDEO_EXTS:
        raise HTTPException(status_code=403, detail="អនុញ្ញាតតែឯកសារវីដេអូប៉ុណ្ណោះ។")
    if not is_path_allowed(path):
        raise HTTPException(status_code=403, detail="មិនអនុញ្ញាតចូលលើផ្លូវនេះឡើយ។")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារវីដេអូឡើយ។")
    return FileResponse(path)

@app.get("/temp/{filename}")
def get_temp_file(filename: str):
    """Serve generated temp assets (uploaded videos, thumbnails) from the writable temp dir."""
    safe_name = os.path.basename(filename)
    target = os.path.join(get_temp_dir(), safe_name)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារឡើយ។")
    res = FileResponse(target)
    res.headers["Cache-Control"] = "no-store"
    return res

# Serve index.html with no-cache headers to ensure immediate UI updates
@app.get("/")
def get_index():
    index_file = os.path.join(public_dir, "index.html")
    res = FileResponse(index_file)
    res.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    res.headers["Pragma"] = "no-cache"
    res.headers["Expires"] = "0"
    return res

@app.get("/{filename}")
def get_static_asset(filename: str):
    """Serve the bundled static assets (favicon, lucide icons). Never shadows /api/* or /temp/*."""
    if filename not in SERVED_STATIC_FILES:
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារឡើយ។")
    target = os.path.join(public_dir, filename)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារឡើយ។")
    return FileResponse(target)

if __name__ == "__main__":
    import uvicorn
    import webbrowser
    import socket
    
    def find_free_port(preferred_port: int = 8765) -> int:
        env_port = os.environ.get("SPLITIFY_PORT")
        if env_port and env_port.isdigit():
            return int(env_port)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', preferred_port))
                return preferred_port
        except OSError:
            pass
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    port = find_free_port(8765)
    app_url = f"http://127.0.0.1:{port}"

    # Function to open Chrome, Edge, or default browser after server starts
    def open_app(url: str):
        time.sleep(1.2)
        
        # 1. Prioritize Google Chrome
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
        ]
        for path in chrome_paths:
            if os.path.exists(path):
                subprocess.Popen([path, f"--app={url}"])
                return

        # 2. Fallback to Microsoft Edge
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        ]
        for path in edge_paths:
            if os.path.exists(path):
                subprocess.Popen([path, f"--app={url}"])
                return

        # 3. Fallback to default system browser
        webbrowser.open(url)

    # Launch browser/app in background thread
    threading.Thread(target=open_app, args=(app_url,), daemon=True).start()

    # Run uvicorn in the main thread so signals and asyncio event loops function properly
    # Disable default log_config to prevent NoneType.isatty error in --noconsole mode
    print(f"[Splitify] Running on {app_url}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)
