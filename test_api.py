import os
import sys
import subprocess
import time
import json
import urllib.request
import shutil
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from test_helpers import get_ffmpeg_bin, get_ffprobe_bin, start_test_server

def run_integration_test():
    print("=== STARTING SPLITIFY INTEGRATION TEST (SPLIT & MERGE) ===")

    API_PORT = int(os.environ.get("SPLITIFY_PORT", 8769))
    base_url = f"http://127.0.0.1:{API_PORT}"

    # Start server in background thread for testing
    print(f"Starting test server on port {API_PORT}...")
    start_test_server(API_PORT)

    # 1. Create two synthetic test videos
    test_video_1 = os.path.abspath("test_input_1.mp4")
    test_video_2 = os.path.abspath("test_input_2.mp4")

    print(f"1. Generating synthetic test videos...")
    gen_cmd_1 = [
        get_ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=6:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
        "-c:v", "libx264", "-c:a", "aac",
        "-t", "6",
        test_video_1
    ]
    gen_cmd_2 = [
        get_ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=4:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=4",
        "-c:v", "libx264", "-c:a", "aac",
        "-t", "4",
        test_video_2
    ]

    try:
        subprocess.run(gen_cmd_1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(gen_cmd_2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print("[OK] Test videos generated (6s and 4s).")
    except Exception as e:
        print(f"[FAIL] Failed to generate test videos: {e}")
        return False

    # 2. Test /api/load-video
    print(f"\n2. Testing /api/load-video endpoint...")
    try:
        req = urllib.request.Request(
            f"{base_url}/api/load-video",
            data=json.dumps({"path": test_video_1}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            assert res_data["success"] is True
            assert res_data["filename"] == "test_input_1.mp4"
            assert abs(res_data["duration"] - 6.0) < 0.2
            assert res_data["resolution"] == "640x360"
            print("[OK] /api/load-video verified successfully!")
    except Exception as e:
        print(f"[FAIL] /api/load-video test failed: {e}")
        return False

    # 3. Test /api/split
    print("\n3. Testing /api/split endpoint...")
    output_split_dir = os.path.abspath("output_split_test")
    split_parts = [
        {"partNum": 1, "start": 0.0, "end": 3.0, "duration": 3.0},
        {"partNum": 2, "start": 3.0, "end": 6.0, "duration": 3.0}
    ]
    split_payload = json.dumps({
        "videoPath": test_video_1,
        "mode": "parts",
        "overlapSec": 0,
        "outputFolder": output_split_dir,
        "outputPrefix": "split_part",
        "parts": split_parts
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{base_url}/api/split",
            data=split_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            assert res_data["success"] is True
            print("[OK] /api/split submitted successfully!")
    except Exception as e:
        print(f"[FAIL] /api/split failed: {e}")
        return False

    # Monitor Split progress
    print("Monitoring Split progress...")
    try:
        req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
        with urllib.request.urlopen(req) as response:
            for line in response:
                line_str = line.decode().strip()
                if line_str.startswith("data:"):
                    state = json.loads(line_str[5:].strip())
                    print(f"  Split Status: {state['status']} | Progress: {state['progress_percent']}%")
                    if state["status"] in ["completed", "error", "canceled"]:
                        assert state["status"] == "completed"
                        break
        print("[OK] Split completed successfully!")
    except Exception as e:
        print(f"[FAIL] Split progress failed: {e}")
        return False

    # Verify split output files
    expected_split_files = ["Part_1_split_part.mp4", "Part_2_split_part.mp4"]
    for fn in expected_split_files:
        fp = os.path.join(output_split_dir, fn)
        assert os.path.exists(fp) and os.path.getsize(fp) > 0
    print("[OK] Split output files verified!")

    # 4. Test /api/check-merge-compatibility
    print("\n4. Testing /api/check-merge-compatibility endpoint...")
    try:
        req = urllib.request.Request(
            f"{base_url}/api/check-merge-compatibility",
            data=json.dumps({"videoPaths": [test_video_1, test_video_2]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            assert res_data["success"] is True
            assert res_data["is_compatible"] is True
            assert res_data["recommended_mode"] == "lossless"
            assert len(res_data["videos"]) == 2
            assert abs(res_data["total_duration"] - 10.0) < 0.2
            print("[OK] /api/check-merge-compatibility verified (Lossless compatible)!")
    except Exception as e:
        print(f"[FAIL] /api/check-merge-compatibility failed: {e}")
        return False

    # 5. Test /api/merge
    print("\n5. Testing /api/merge endpoint...")
    output_merge_dir = os.path.abspath("output_merge_test")
    merge_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_2],
        "outputFolder": output_merge_dir,
        "outputFilename": "test_merged.mp4",
        "mode": "auto"
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"{base_url}/api/merge",
            data=merge_payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode())
            assert res_data["success"] is True
            print("[OK] /api/merge submitted successfully!")
    except Exception as e:
        print(f"[FAIL] /api/merge failed: {e}")
        return False

    # Monitor Merge progress
    print("Monitoring Merge progress...")
    try:
        req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
        with urllib.request.urlopen(req) as response:
            for line in response:
                line_str = line.decode().strip()
                if line_str.startswith("data:"):
                    state = json.loads(line_str[5:].strip())
                    print(f"  Merge Status: {state['status']} | Progress: {state['progress_percent']}%")
                    if state["status"] in ["completed", "error", "canceled"]:
                        assert state["status"] == "completed"
                        break
        print("[OK] Merge completed successfully!")
    except Exception as e:
        print(f"[FAIL] Merge progress failed: {e}")
        return False

    # Verify merged output file
    merged_file_path = os.path.join(output_merge_dir, "test_merged.mp4")
    assert os.path.exists(merged_file_path) and os.path.getsize(merged_file_path) > 0
    print(f"[OK] Merged output file verified ({os.path.getsize(merged_file_path)} bytes)!")

    # Verify merged duration using ffprobe
    probe_cmd = [
        get_ffprobe_bin(), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        merged_file_path
    ]
    merged_dur = float(subprocess.check_output(probe_cmd, text=True).strip())
    print(f"Merged video duration: {merged_dur:.2f}s (expected ~10.0s)")
    assert abs(merged_dur - 10.0) < 0.5
    print("[OK] Merged duration verified!")

    # 6. Test Smart Re-encode Merge with different resolutions
    print("\n6. Testing Smart Re-encode Merge with different resolutions (640x360 & 320x240)...")
    test_video_diff_res = os.path.abspath("test_input_diff_res.mp4")
    gen_cmd_diff = [
        get_ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=800:duration=3",
        "-c:v", "libx264", "-c:a", "aac",
        "-t", "3",
        test_video_diff_res
    ]
    subprocess.run(gen_cmd_diff, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    # Check compatibility - should be incompatible (requires auto fix or reencode)
    req = urllib.request.Request(
        f"{base_url}/api/check-merge-compatibility",
        data=json.dumps({"videoPaths": [test_video_1, test_video_diff_res]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True
        assert res_data["is_compatible"] is False
        assert res_data["recommended_mode"] in ["auto", "reencode"]
        print("[OK] Smart compatibility check detected discrepancies correctly!")

    # Execute Re-encode Merge
    reencode_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_diff_res],
        "outputFolder": output_merge_dir,
        "outputFilename": "test_reencoded.mp4",
        "mode": "reencode"
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/merge",
        data=reencode_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True

    # Monitor Re-encode progress
    req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
    with urllib.request.urlopen(req) as response:
        for line in response:
            line_str = line.decode().strip()
            if line_str.startswith("data:"):
                state = json.loads(line_str[5:].strip())
                if state["status"] in ["completed", "error", "canceled"]:
                    assert state["status"] == "completed"
                    break
    reencoded_file = os.path.join(output_merge_dir, "test_reencoded.mp4")
    assert os.path.exists(reencoded_file) and os.path.getsize(reencoded_file) > 0
    print("[OK] Smart Re-encode merge succeeded and generated output!")

    # 6b. Test Selective Auto-Fix Merge (Mismatched FPS)
    print("\n6b. Testing Selective Auto-Fix Merge with Mismatched FPS (30fps & 25fps)...")
    test_video_3 = os.path.abspath("test_input_3.mp4")
    gen_cmd_3 = [
        get_ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=3",
        "-c:v", "libx264", "-c:a", "aac",
        "-t", "3",
        test_video_3
    ]
    subprocess.run(gen_cmd_3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    autofix_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_diff_res, test_video_3],
        "outputFolder": output_merge_dir,
        "outputFilename": "test_smartfix.mp4",
        "mode": "auto"
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/api/merge",
        data=autofix_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True

    req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
    with urllib.request.urlopen(req) as response:
        for line in response:
            line_str = line.decode().strip()
            if line_str.startswith("data:"):
                state = json.loads(line_str[5:].strip())
                if state["status"] in ["completed", "error", "canceled"]:
                    assert state["status"] == "completed"
                    break
    autofix_file = os.path.join(output_merge_dir, "test_smartfix.mp4")
    assert os.path.exists(autofix_file) and os.path.getsize(autofix_file) > 0
    print("[OK] Selective Auto-Fix merge succeeded and produced verified output!")

    # 6c. Testing Multi-Part Merging (Split by FPS, by Count, by Parts, by Custom)
    print("\n6c. Testing Multi-Part Merging & Split by FPS...")
    # Preview endpoint test
    preview_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_2, test_video_diff_res, test_video_3],
        "partitionMode": "fps",
        "outputFilename": "MultiPart_Test.mp4"
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/preview-merge-partitions",
        data=preview_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        preview_data = json.loads(response.read().decode())
        assert preview_data["success"] is True
        # video_1 is 30fps, video_2 is 30fps, diff_res is 25fps, video_3 is 30fps
        # Expect 3 partitions: [0..2] 30fps, [2..3] 25fps, [3..4] 30fps
        assert len(preview_data["partitions"]) == 3
        assert preview_data["partitions"][0]["episode_count"] == 2
        assert preview_data["partitions"][1]["episode_count"] == 1
        assert preview_data["partitions"][2]["episode_count"] == 1
        print("[OK] Partition Preview correctly split by FPS into 3 parts!")

    # Multi-Part Merge execution test with "fps"
    multipart_merge_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_2, test_video_diff_res, test_video_3],
        "outputFolder": output_merge_dir,
        "outputFilename": "MultiPart_FPS.mp4",
        "enableMultiPart": True,
        "partitionMode": "fps"
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/merge",
        data=multipart_merge_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True

    req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
    with urllib.request.urlopen(req) as response:
        for line in response:
            line_str = line.decode().strip()
            if line_str.startswith("data:"):
                state = json.loads(line_str[5:].strip())
                if state["status"] in ["completed", "error", "canceled"]:
                    assert state["status"] == "completed"
                    break

    # Verify generated part files
    part1_path = os.path.join(output_merge_dir, "MultiPart_FPS_Part_1.mp4")
    part2_path = os.path.join(output_merge_dir, "MultiPart_FPS_Part_2.mp4")
    part3_path = os.path.join(output_merge_dir, "MultiPart_FPS_Part_3.mp4")
    assert os.path.exists(part1_path) and os.path.getsize(part1_path) > 0
    assert os.path.exists(part2_path) and os.path.getsize(part2_path) > 0
    assert os.path.exists(part3_path) and os.path.getsize(part3_path) > 0
    print("[OK] Multi-Part Merge by FPS generated all 3 part files successfully!")

    # 7. Test Cross-Pipeline Bridge (Merge -> Load Video)
    print("\n7. Testing Cross-Pipeline Bridge: Loading merged video back into /api/load-video...")
    req = urllib.request.Request(
        f"{base_url}/api/load-video",
        data=json.dumps({"path": merged_file_path}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True
        assert res_data["filename"] == "test_merged.mp4"
        print("[OK] Cross-Pipeline: Merged video loaded into Split editor successfully!")

    # 8. Test Task Cancellation (/api/cancel)
    print("\n8. Testing Task Cancellation (/api/cancel)...")
    # Start a merge with reencode (takes longer)
    req = urllib.request.Request(
        f"{base_url}/api/merge",
        data=reencode_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    urllib.request.urlopen(req)
    time.sleep(0.5)
    # Cancel it
    req_cancel = urllib.request.Request(f"{base_url}/api/cancel", method="POST")
    with urllib.request.urlopen(req_cancel) as cancel_res:
        cancel_data = json.loads(cancel_res.read().decode())
        assert cancel_data["success"] is True
    # Wait for cancelled task thread to exit cleanly
    time.sleep(1.0)
    print("[OK] /api/cancel successfully cancelled active task!")

    # 9. Test Khmer Unicode Filenames
    print("\n9. Testing Khmer Unicode Filename in Merge...")
    khmer_output_name = "វីដេអូតេស្ត_រួមគ្នា_ខ្មែរ.mp4"
    khmer_merge_payload = json.dumps({
        "videoPaths": [test_video_1, test_video_2],
        "outputFolder": output_merge_dir,
        "outputFilename": khmer_output_name,
        "mode": "auto"
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/merge",
        data=khmer_merge_payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        res_data = json.loads(response.read().decode())
        assert res_data["success"] is True

    req = urllib.request.Request(f"{base_url}/api/progress", method="GET")
    with urllib.request.urlopen(req) as response:
        for line in response:
            line_str = line.decode().strip()
            if line_str.startswith("data:"):
                state = json.loads(line_str[5:].strip())
                if state["status"] in ["completed", "error", "canceled"]:
                    print(f"  Step 9 final state: {state['status']}, error: {state.get('error_message')}")
                    assert state["status"] == "completed"
                    break
    khmer_file_path = os.path.join(output_merge_dir, khmer_output_name)
    assert os.path.exists(khmer_file_path) and os.path.getsize(khmer_file_path) > 0
    print(f"[OK] Khmer Unicode filename handled properly: {khmer_output_name}")

    # 10. Cleanup
    print("\n10. Cleaning up test artifacts...")
    time.sleep(0.5)
    for p in [test_video_1, test_video_2, test_video_3, test_video_diff_res]:
        for _ in range(5):
            try:
                if os.path.exists(p):
                    os.remove(p)
                break
            except Exception:
                time.sleep(0.4)
    if os.path.exists(output_split_dir):
        shutil.rmtree(output_split_dir, ignore_errors=True)
    if os.path.exists(output_merge_dir):
        shutil.rmtree(output_merge_dir, ignore_errors=True)
    print("[OK] Cleanup completed.")

    print("\n=== ALL TESTS PASSED SUCCESSFULLY! ===")
    return True

if __name__ == "__main__":
    success = run_integration_test()
    sys.exit(0 if success else 1)

