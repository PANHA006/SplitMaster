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

    # 2. Construct PyInstaller command
    # --onefile: package into a single .exe
    # --noconsole: hide the black terminal console window on startup
    # --add-data "public;public": bundle the public static directory
    # --name "Splitify": name the output binary
    pyinstaller_cmd = [
        "python", "-m", "PyInstaller",
        "--noconsole",
        "--onefile",
        "--add-data", "public;public",
        "--name", "Splitify",
        "main.py"
    ]
    
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
