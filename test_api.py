import os
import sys
import subprocess
import time
import json
import urllib.request

def run_integration_test():
    print("=== STARTING SPLITIFY INTEGRATION TEST ===")
    
    # 1. Create a 10-second synthetic test video using ffmpeg
    test_video_path = os.path.abspath("test_input.mp4")
    print(f"1. Generating synthetic test video at: {test_video_path}")
    
    # Command to generate 10s video (640x360, H264)
    gen_cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "testsrc=duration=10:size=640x360:rate=30",
        "-c:v", "libx264",
        "-t", "10",
        test_video_path
    ]
    
    try:
        subprocess.run(gen_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print("[OK] Test video generated successfully.")
    except Exception as e:
        print(f"[FAIL] Failed to generate test video: {e}")
        return False

    # 2. Call /api/load-video
    print("\n2. Testing /api/load-video endpoint...")
    load_url = "http://127.0.0.1:8000/api/load-video"
    payload = json.dumps({"path": test_video_path}).encode("utf-8")
    
    try:
        req = urllib.request.Request(
            load_url, 
            data=payload, 
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            # Safely print keys/booleans to avoid Khmer Unicode console errors
            print("Response keys:", list(res_data.keys()))
            print("Response success:", res_data.get("success"))
            
            assert res_data["success"] is True
            assert res_data["filename"] == "test_input.mp4"
            assert abs(res_data["duration"] - 10.0) < 0.1
            assert res_data["resolution"] == "640x360"
            print("[OK] /api/load-video verified successfully!")
    except Exception as e:
        print(f"[FAIL] /api/load-video test failed: {e}")
        return False

    # 3. Call /api/split
    # We will split it into 3 parts, with 1 second overlap
    print("\n3. Testing /api/split endpoint...")
    split_url = "http://127.0.0.1:8000/api/split"
    
    parts = [
        {"partNum": 1, "start": 0.0, "end": 4.33, "duration": 4.33},
        {"partNum": 2, "start": 3.33, "end": 7.66, "duration": 4.33},
        {"partNum": 3, "start": 6.66, "end": 10.00, "duration": 3.34}
    ]
    
    split_payload = json.dumps({
        "videoPath": test_video_path,
        "mode": "parts",
        "overlapSec": 1,
        "outputFolder": os.path.abspath("output_test"),
        "outputPrefix": "test_rose",
        "parts": parts
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            split_url, 
            data=split_payload, 
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            print("Response success:", res_data.get("success"))
            assert res_data["success"] is True
            print("[OK] /api/split request submitted successfully!")
    except Exception as e:
        print(f"[FAIL] /api/split submission failed: {e}")
        return False

    # 4. Monitor /api/progress
    print("\n4. Monitoring progress...")
    progress_url = "http://127.0.0.1:8000/api/progress"
    
    try:
        req = urllib.request.Request(progress_url, method="GET")
        with urllib.request.urlopen(req) as response:
            for line in response:
                line_str = line.decode().strip()
                if line_str.startswith("data:"):
                    state = json.loads(line_str[5:].strip())
                    # Clean output to avoid Khmer encoding problems
                    print(f"Status: {state['status']} | Progress: {state['progress_percent']}% | Current Part: {state['current_part']}/{state['total_parts']}")
                    if state["status"] in ["completed", "error", "canceled"]:
                        assert state["status"] == "completed", f"Expected completed, got {state['status']}"
                        break
    except Exception as e:
        print(f"[FAIL] Progress monitoring failed: {e}")
        return False

    # 5. Verify Output Files
    print("\n5. Verifying output files...")
    output_dir = os.path.abspath("output_test")
    expected_files = [
        "Part_1_test_rose.mp4",
        "Part_2_test_rose.mp4",
        "Part_3_test_rose.mp4"
    ]
    
    for filename in expected_files:
        filepath = os.path.join(output_dir, filename)
        if os.path.exists(filepath):
            size = os.path.getsize(filepath)
            print(f"[OK] Found output file: {filename} ({size} bytes)")
        else:
            print(f"[FAIL] Missing output file: {filename}")
            return False

    # Cleanup test files
    print("\n6. Cleaning up test inputs and outputs...")
    try:
        os.remove(test_video_path)
        for filename in expected_files:
            os.remove(os.path.join(output_dir, filename))
        os.rmdir(output_dir)
        print("[OK] Cleanup completed.")
    except Exception as e:
        print(f"[WARN] Cleanup failed: {e}")

    print("\n=== INTEGRATION TEST PASSED SUCCESSFULLY! ===")
    return True

if __name__ == "__main__":
    success = run_integration_test()
    sys.exit(0 if success else 1)
