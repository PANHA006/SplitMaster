import os
import sys
import subprocess
import shutil

def build_app():
    print("=== STARTING SPLITIFY COMPILATION PROCESS ===")
    
    # 1. Clean previous build files if they exist
    print("Cleaning up old build folders...")
    for folder in ["build", "dist"]:
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder)
                print(f"[OK] Removed {folder}/")
            except Exception as e:
                print(f"[WARN] Failed to remove {folder}/: {e}")
                
    spec_file = "Splitify.spec"
    if os.path.exists(spec_file):
        try:
            os.remove(spec_file)
            print("[OK] Removed old spec file.")
        except Exception as e:
            print(f"[WARN] Failed to remove {spec_file}: {e}")

    # 2. Locate ffmpeg and ffprobe binaries to bundle
    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe or not os.path.exists(ffmpeg_exe):
        winget_ffmpeg = r"C:\Users\DARO\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
        if os.path.exists(winget_ffmpeg):
            ffmpeg_exe = winget_ffmpeg

    ffprobe_exe = shutil.which("ffprobe")
    if not ffprobe_exe or not os.path.exists(ffprobe_exe):
        winget_ffprobe = r"C:\Users\DARO\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin\ffprobe.exe"
        if os.path.exists(winget_ffprobe):
            ffprobe_exe = winget_ffprobe

    # 3. Construct PyInstaller command
    # --onefile: package into a single .exe
    # --noconsole: hide the black terminal console window on startup
    # --add-data "public;public": bundle the public static directory
    # --add-binary: bundle ffmpeg and ffprobe binaries into the single file
    pyinstaller_cmd = [
        "python", "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--add-data", "public;public",
        "--name", "Splitify"
    ]

    if ffmpeg_exe and os.path.exists(ffmpeg_exe):
        print(f"[OK] Bundling ffmpeg binary: {ffmpeg_exe}")
        pyinstaller_cmd.extend(["--add-binary", f"{ffmpeg_exe};."])
    if ffprobe_exe and os.path.exists(ffprobe_exe):
        print(f"[OK] Bundling ffprobe binary: {ffprobe_exe}")
        pyinstaller_cmd.extend(["--add-binary", f"{ffprobe_exe};."])

    pyinstaller_cmd.append("main.py")
    
    print(f"\nRunning command: {' '.join(pyinstaller_cmd)}")
    
    try:
        # Run PyInstaller and redirect outputs to console
        result = subprocess.run(pyinstaller_cmd, check=True)
        print("\n=== COMPILATION COMPLETED SUCCESSFULLY! ===")
        print("Your standalone executable is located at: dist/Splitify.exe")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n[FAIL] PyInstaller compilation failed with error: {e}")
        return False
    except FileNotFoundError:
        print("\n[FAIL] PyInstaller is not installed or not in the PATH.")
        return False

if __name__ == "__main__":
    success = build_app()
    sys.exit(0 if success else 1)
