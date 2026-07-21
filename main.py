import os
import sys
import io
import subprocess

# Fix PyInstaller --noconsole mode where sys.stdout and sys.stderr are None
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()
import threading
import time
import json
import math
import shutil
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Splitify Backend")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State for video splitting task
split_state = {
    "is_running": False,
    "progress_percent": 0,
    "current_part": 0,
    "total_parts": 0,
    "current_filename": "",
    "start_time": 0,
    "elapsed_seconds": 0,
    "eta_seconds": 0,
    "cancel_requested": False,
    "error_message": "",
    "process": None
}

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
    overlapSec: int
    outputFolder: str
    outputPrefix: str
    parts: list[SplitPart]

# Determine base directory for resources
if getattr(sys, 'frozen', False):
    # Running in a PyInstaller bundle
    base_dir = sys._MEIPASS
else:
    # Running in a normal Python environment
    base_dir = os.path.dirname(os.path.abspath(__file__))

public_dir = os.path.join(base_dir, "public")

# Serve UI static files
# Make sure public folder and output folder exist
os.makedirs(os.path.join(public_dir, "temp"), exist_ok=True)
os.makedirs("output", exist_ok=True)

# Helper to format seconds to hh:mm:ss
def format_seconds(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}"

def get_ffmpeg_cmd() -> str:
    if getattr(sys, 'frozen', False):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
    which_path = shutil.which("ffmpeg")
    if which_path and os.path.exists(which_path):
        return which_path
    winget_path = r"C:\Users\DARO\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
    if os.path.exists(winget_path):
        return winget_path
    return "ffmpeg"

def get_ffprobe_cmd() -> str:
    if getattr(sys, 'frozen', False):
        bundled = os.path.join(sys._MEIPASS, "ffprobe.exe")
        if os.path.exists(bundled):
            return bundled
    which_path = shutil.which("ffprobe")
    if which_path and os.path.exists(which_path):
        return which_path
    winget_path = r"C:\Users\DARO\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffprobe.exe"
    if os.path.exists(winget_path):
        return winget_path
    return "ffprobe"

@app.get("/api/config")
def get_config():
    return {
        "default_output_folder": os.path.abspath("output")
    }

@app.post("/api/select-file")
def select_file():
    try:
        # PowerShell script to open File Open Dialog on Windows (STA mode)
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$FileBrowser = New-Object System.Windows.Forms.OpenFileDialog; "
            "$FileBrowser.Filter = 'Video Files|*.mp4;*.mkv;*.avi;*.mov;*.webm;*.flv|All Files|*.*'; "
            "$FileBrowser.Title = 'Select Video File'; "
            "$Show = $FileBrowser.ShowDialog(); "
            "If ($Show -eq 'OK') { Write-Output $FileBrowser.FileName }"
        )
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
        file_path = subprocess.check_output(cmd, text=True).strip()
        
        if file_path and os.path.exists(file_path):
            return {"success": True, "path": file_path}
        return {"success": True, "path": ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open file dialog: {str(e)}")

@app.post("/api/select-folder")
def select_folder():
    try:
        # PowerShell script to open Folder Browser Dialog on Windows (STA mode)
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$FolderBrowser = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$FolderBrowser.Description = 'Select Output Folder'; "
            "$Show = $FolderBrowser.ShowDialog(); "
            "If ($Show -eq 'OK') { Write-Output $FolderBrowser.SelectedPath }"
        )
        cmd = ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script]
        folder_path = subprocess.check_output(cmd, text=True).strip()
        
        if folder_path and os.path.exists(folder_path):
            return {"success": True, "path": folder_path}
        return {"success": True, "path": ""}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot open folder dialog: {str(e)}")

@app.post("/api/upload")
def upload_file(file: UploadFile = File(...)):
    try:
        temp_dir = os.path.join("public", "temp")
        os.makedirs(temp_dir, exist_ok=True)
        
        # Clean up old uploaded files in public/temp to save disk space
        for f in os.listdir(temp_dir):
            if f.endswith(os.path.splitext(file.filename)[1]) or f == "thumbnail.jpg":
                try:
                    os.remove(os.path.join(temp_dir, f))
                except Exception:
                    pass

        temp_path = os.path.join(temp_dir, file.filename)
        with open(temp_path, "wb") as buffer:
            # Read in chunks of 1MB to handle large files efficiently
            while chunk := file.file.read(1024 * 1024):
                buffer.write(chunk)
                
        return {"success": True, "path": os.path.abspath(temp_path)}
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
        duration_out = subprocess.check_output(duration_cmd, text=True).strip()
        duration = float(duration_out)

        # 2. Get Resolution using ffprobe
        res_cmd = [
            get_ffprobe_cmd(), "-v", "error", 
            "-select_streams", "v:0", 
            "-show_entries", "stream=width,height", 
            "-of", "csv=s=x:p=0", 
            video_path
        ]
        resolution = subprocess.check_output(res_cmd, text=True).strip()

        # 3. Get File Size
        size_bytes = os.path.getsize(video_path)
        size_gb = size_bytes / (1024 * 1024 * 1024)
        size_text = f"{size_gb:.2f} GB" if size_gb >= 1.0 else f"{size_bytes / (1024 * 1024):.2f} MB"

        # 4. Generate Thumbnail (at 5s mark or 0s if short)
        thumb_secs = 5.0 if duration >= 5.0 else 0.0
        thumb_relative_path = "temp/thumbnail.jpg"
        thumb_dest = os.path.join("public", thumb_relative_path)
        
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

# Background video splitting execution
def execute_split(req: SplitRequest):
    global split_state
    
    video_path = req.videoPath.strip()
    if (video_path.startswith('"') and video_path.endswith('"')) or (video_path.startswith("'") and video_path.endswith("'")):
        video_path = video_path[1:-1]
        
    output_folder = req.outputFolder.strip()
    os.makedirs(output_folder, exist_ok=True)
    
    ext = os.path.splitext(video_path)[1]
    if not ext:
        ext = ".mp4"

    total_parts = len(req.parts)
    output_files = []

    with state_lock:
        split_state["is_running"] = True
        split_state["progress_percent"] = 0
        split_state["current_part"] = 0
        split_state["total_parts"] = total_parts
        split_state["status"] = "processing"
        split_state["error_message"] = ""
        split_state["output_files"] = []
        split_state["output_folder"] = output_folder
        split_state["start_time"] = time.time()
        split_state["elapsed_time"] = 0.0

    try:
        for i, part in enumerate(req.parts):
            # Check for cancellation before starting next part
            with state_lock:
                if split_state["status"] == "canceled":
                    break

            part_num = part.partNum
            start = part.start
            end = part.end
            
            # Format filename safely: Part_1_កុលាបក្រហម.mp4
            part_filename = f"Part_{part_num}_{req.outputPrefix}{ext}"
            part_output_path = os.path.join(output_folder, part_filename)

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
                "-avoid_negative_ts", "make_zero",
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
                if process.returncode != 0 and split_state["status"] != "canceled":
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
            with state_lock:
                split_state["progress_percent"] = percent
                split_state["output_files"] = list(output_files)
                split_state["elapsed_time"] = time.time() - split_state["start_time"]

        # If we exited loop successfully without cancel
        with state_lock:
            if split_state["status"] == "processing":
                split_state["status"] = "completed"
                split_state["is_running"] = False
                split_state["progress_percent"] = 100
                split_state["active_process"] = None

    except Exception as e:
        with state_lock:
            split_state["status"] = "error"
            split_state["is_running"] = False
            split_state["error_message"] = str(e)
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
        split_state["error_message"] = ""
        split_state["output_files"] = []
        split_state["output_folder"] = req.outputFolder.strip()
        split_state["start_time"] = time.time()
        split_state["elapsed_time"] = 0.0
            
    background_tasks.add_task(execute_split, req)
    return {"success": True, "message": "ការបំបែកវីដេអូចាប់ផ្ដើមក្នុង Background"}

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
                    "error_message": split_state["error_message"],
                    "output_files": split_state["output_files"],
                    "output_folder": split_state["output_folder"],
                    "elapsed_time": int(split_state["elapsed_time"]),
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
    with state_lock:
        if not split_state["is_running"] and split_state["status"] != "processing":
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
def open_folder():
    global split_state
    folder = split_state["output_folder"]
    if not folder or not os.path.exists(folder):
        # Fallback to local output folder
        folder = os.path.abspath("output")
        
    os.makedirs(folder, exist_ok=True)
    
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
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="មិនរកឃើញឯកសារវីដេអូឡើយ។")
    return FileResponse(path)

# Mount static files at the root
app.mount("/", StaticFiles(directory=public_dir, html=True), name="public")

if __name__ == "__main__":
    import uvicorn
    import threading
    import subprocess
    import webbrowser
    import time
    
    # Function to open browser or Edge App window after server starts
    def open_app():
        time.sleep(1.2)
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        ]
        edge_exe = None
        for path in edge_paths:
            if os.path.exists(path):
                edge_exe = path
                break

        if edge_exe:
            subprocess.Popen([edge_exe, "--app=http://127.0.0.1:8000"])
        else:
            webbrowser.open("http://127.0.0.1:8000")

    # Launch browser/app in background thread
    threading.Thread(target=open_app, daemon=True).start()

    # Run uvicorn in the main thread so signals and asyncio event loops function properly
    # Disable default log_config to prevent NoneType.isatty error in --noconsole mode
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)
