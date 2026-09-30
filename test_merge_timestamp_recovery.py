"""Regression tests for the merge timestamp-recovery work.

Reproduces the real-world failure reported by users:
    [aost#0:1/copy] Non-monotonic DTS ...
    [vost#0:0/copy] Error submitting a packet to the muxer: Invalid data found when processing input
    [out#0/mp4] Task finished with error code: -1094995529 (AVERROR_INVALIDDATA)

Covers:
  1. The uniformity check now detects start_time / timebase mismatches.
  2. Multi-part merge of such a batch still completes (auto-recovery / fallback).
  3. A hopeless failure sets status="error" (never sticks on "processing") and leaves no
     half-written output file behind.
  4. FFmpeg failures are reported with a friendly Khmer message.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from test_helpers import get_ffmpeg_bin, get_ffprobe_bin, start_test_server

PORT = int(os.environ.get("SPLITIFY_TEST_PORT", 8781))
API_URL = f"http://127.0.0.1:{PORT}/api/merge"

TEST_DIR = os.path.abspath("test_timestamp_recovery")
OUT_DIR = os.path.join(TEST_DIR, "out")
DURATION = 3

results = []


def check(label, condition, extra=""):
    results.append(bool(condition))
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {extra}" if extra else ""))


def make_clip(path, duration=DURATION, ts_offset=None, video_timescale=None):
    """Synthetic clip; ts_offset / video_timescale mimic episodes made by other tools."""
    cmd = [
        get_ffmpeg_bin(), "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x360:rate=25",
        "-f", "lavfi", "-i", f"sine=frequency=1000:duration={duration}",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-c:a", "aac", "-t", str(duration),
    ]
    if ts_offset is not None:
        cmd += ["-output_ts_offset", str(ts_offset)]
    if video_timescale is not None:
        cmd += ["-video_track_timescale", str(video_timescale)]
    cmd.append(path)
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return path


def probe_duration(path):
    out = subprocess.check_output(
        [get_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        encoding="utf-8", errors="replace"
    ).strip()
    try:
        return float(out)
    except Exception:
        return 0.0


def post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_terminal_state(app_module, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = dict(app_module.split_state)
        if state.get("status") in ("completed", "error", "canceled"):
            return state
        time.sleep(0.5)
    return dict(app_module.split_state)


def cleanup():
    for root, _dirs, files in os.walk(TEST_DIR, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(root, f))
            except Exception:
                pass
        try:
            os.rmdir(root)
        except Exception:
            pass


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    server = start_test_server(PORT)
    import main as app_module

    print("=" * 74)
    print("MERGE TIMESTAMP RECOVERY - REGRESSION SUITE")
    print("=" * 74)

    try:
        # --- Build a batch that mimics the reported problem ------------------------
        clean_a = make_clip(os.path.join(TEST_DIR, "ep1.mp4"))
        odd_start = make_clip(os.path.join(TEST_DIR, "ep2.mp4"), ts_offset=2.0)
        odd_tb = make_clip(os.path.join(TEST_DIR, "ep3.mp4"), video_timescale=30000)
        clean_b = make_clip(os.path.join(TEST_DIR, "ep4.mp4"))
        videos = [clean_a, odd_start, odd_tb, clean_b]
        print(f"Created {len(videos)} synthetic episodes in {TEST_DIR}\n")

        # --- 1. Uniformity detection ----------------------------------------------
        infos = [app_module.probe_single_video(p) for p in videos]
        _baseline, mismatches, reasons = app_module.analyze_merge_videos(infos)
        check("detect 2 mismatching episodes", len(mismatches) == 2, f"mismatches={mismatches}")
        reason_blob = " ".join(reasons.values())
        check("reason mentions start offset", "ចំណុចចាប់ផ្ដើម" in reason_blob, reason_blob[:140])
        check("timebase or start mismatch flagged", "Timebase" in reason_blob or "ចំណុចចាប់ផ្ដើម" in reason_blob)

        # --- 2. Multi-part merge of the same batch must still succeed -------------
        out_folder = os.path.join(OUT_DIR, "multipart")
        os.makedirs(out_folder, exist_ok=True)
        post_json(API_URL, {
            "videoPaths": videos,
            "outputFolder": out_folder,
            "outputFilename": "Recovered_Video.mp4",
            "enableMultiPart": True,
            "partitionMode": "parts",
            "targetPartCount": 2,
            "episodesPerPart": 20,
        })
        state = wait_for_terminal_state(app_module)
        check("multi-part merge finished", state.get("status") == "completed",
              f"status={state.get('status')} error={str(state.get('error_message', ''))[:160]}")
        produced = sorted(f for f in os.listdir(out_folder) if f.endswith(".mp4"))
        check("both parts generated", len(produced) == 2, f"files={produced}")
        for f in produced:
            dur = probe_duration(os.path.join(out_folder, f))
            check(f"{f} is playable", dur > 0.5, f"duration={dur}s")
        leftovers = [f for f in os.listdir(out_folder) if not f.endswith(".mp4")]
        check("no leftover temp/log files", not leftovers, f"leftovers={leftovers}")

        # --- 3. Hopeless failure -> error state, no partial file ------------------
        # 3a. The retry chain itself: every strategy must fail cleanly.
        bad_inputs = [os.path.join("Z:\\missing", f"ghost{i}.mp4") for i in range(2)]
        bad_list = app_module._concat_list_file(bad_inputs, os.path.join(TEST_DIR, "bad_list.txt"))
        out_bad = os.path.join(OUT_DIR, "chain_fail.mp4")
        chain_msg = ""
        raised = False
        try:
            app_module.perform_lossless_merge(
                video_paths=bad_inputs,
                concat_list_path=bad_list,
                out_path=out_bad,
                duration=6.0,
                log_path=os.path.join(TEST_DIR, "chain.log"),
                temp_dir=TEST_DIR,
                tag="unittest",
            )
        except Exception as exc:
            raised = True
            chain_msg = str(exc)
        check("lossless chain raises when every attempt fails", raised, chain_msg[:90])
        check("chain error message is user friendly", ("សម្រាប់" in chain_msg or "MPEG-TS" in chain_msg or "គ្មាន" in chain_msg))
        check("chain leaves no half-written file", not os.path.exists(out_bad))

        # 3b. A job that cannot even start must still release the state machine.
        try:
            post_json(API_URL, {
                "videoPaths": [clean_a, clean_b],
                "outputFolder": "Z:\\definitely_not_writable\\out",
                "outputFilename": "Never.mp4",
                "enableMultiPart": False,
                "mode": "auto",
            })
        except urllib.error.HTTPError:
            pass
        state = wait_for_terminal_state(app_module, timeout=60)
        check("state never sticks on 'processing'", state.get("status") != "processing",
              f"status={state.get('status')}")
        check("state resolved to error/canceled", state.get("status") in ("error", "canceled"),
              f"status={state.get('status')}")
        check("is_running released", state.get("is_running") is False,
              f"is_running={state.get('is_running')}")
        if state.get("status") == "error":
            print(f"       error message -> {str(state.get('error_message', ''))[:200]}")

        # --- 4. Friendly Khmer error messages -------------------------------------
        dts_msg = app_module.explain_ffmpeg_error(
            "[aost#0:1/copy] Non-monotonic DTS; previous: 29662695, current: 29661667 "
            "[vost#0:0/copy] Error submitting a packet to the muxer: Invalid data found when processing input")
        check("Non-monotonic DTS explained in Khmer", "timestamp" in dts_msg and "Lossless" in dts_msg,
              dts_msg[:120])
        check("unknown error returns empty hint", app_module.explain_ffmpeg_error("something odd") == "")

    finally:
        cleanup()
        try:
            server.should_exit = True
        except Exception:
            pass

    print("=" * 74)
    passed = sum(results)
    total = len(results)
    print(f"RESULT: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
