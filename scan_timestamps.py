"""Scan a folder of episodes and report which ones break lossless stream-copy concat.

Why this exists
---------------
FFmpeg's concat demuxer builds one continuous timeline using the *container* duration of each
file. When a file's declared duration is shorter than its real stream end (or its start_time /
timebase differs), the next episode is laid down too early and FFmpeg aborts with:

    [aost#0:1/copy] Non-monotonic DTS; previous: X, current: Y
    [vost#0:0/copy] Error submitting a packet to the muxer: Invalid data found when processing input
    [out#0/mp4] Task finished with error code: -1094995529 (AVERROR_INVALIDDATA)

Usage
-----
    python scan_timestamps.py                 # scans ./input
    python scan_timestamps.py "D:\\Episodes" # scans a folder
    python scan_timestamps.py folder1 folder2 folder3
"""

import json
import os
import subprocess
import sys

from ffmpeg_tools import find_binary

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts"}

FFPROBE_ENTRIES = "format=duration,start_time:stream=codec_type,duration,start_time,time_base,nb_frames,r_frame_rate"


def ffprobe_format(path: str) -> dict:
    cmd = [
        find_binary("ffprobe") or "ffprobe", "-v", "error",
        "-show_entries", FFPROBE_ENTRIES, "-of", "json", path,
    ]
    try:
        return json.loads(subprocess.check_output(cmd, encoding="utf-8", errors="replace") or "{}")
    except Exception:
        return {}


def to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def analyze_file(path: str) -> dict:
    data = ffprobe_format(path)
    fmt = data.get("format", {}) or {}
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"] or \
              [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]

    video = streams[0] if streams else {}
    fmt_dur = to_float(fmt.get("duration"))
    fmt_start = to_float(fmt.get("start_time"))
    stream_dur = to_float(video.get("duration"))
    stream_start = to_float(video.get("start_time"))

    # Real end of the media = start + stream duration (fallback: container duration)
    real_end = (stream_start + stream_dur) if stream_dur else (fmt_start + fmt_dur)
    declared = fmt_dur if fmt_dur else real_end
    drift = real_end - declared  # > 0 means the container lies: concat will overlap the next file

    risks = []
    if abs(fmt_start) > 0.5:
        risks.append(f"start_time={fmt_start:.3f}s")
    if drift > 0.25:
        risks.append(f"container under-declares duration by {drift:.3f}s")
    elif drift < -0.25:
        risks.append(f"container over-declares duration by {abs(drift):.3f}s")

    return {
        "path": path,
        "name": os.path.basename(path),
        "fmt_duration": fmt_dur,
        "fmt_start": fmt_start,
        "stream_duration": stream_dur,
        "time_base": video.get("time_base", ""),
        "drift": drift,
        "risks": risks,
    }


def scan_folder(folder: str) -> list[dict]:
    files = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in VIDEO_EXTS
    )
    return [analyze_file(p) for p in files]


def main(argv: list[str]) -> int:
    folders = argv[1:] or ["input"]
    all_files: list[dict] = []
    for folder in folders:
        if not os.path.isdir(folder):
            print(f"[SKIP] not a folder: {folder}")
            continue
        rows = scan_folder(folder)
        all_files.extend(rows)
        print(f"Scanned {len(rows)} file(s) in {folder}")

    if not all_files:
        print("No video files found.")
        return 1

    starts = {round(r["fmt_start"], 1) for r in all_files}
    timebases = {r["time_base"] for r in all_files if r["time_base"]}

    print("\n" + "=" * 92)
    print(f"{'#':>3}  {'file':<44} {'start':>7} {'duration':>9} {'drift':>8}  notes")
    print("=" * 92)
    cumulative = 0.0
    flagged = []
    for i, r in enumerate(all_files, start=1):
        cumulative += r["fmt_duration"]
        note = "; ".join(r["risks"])
        mark = "  <== CHECK" if r["risks"] else ""
        print(f"{i:>3}  {r['name'][:44]:<44} {r['fmt_start']:>7.3f} {r['fmt_duration']:>9.3f} "
              f"{r['drift']:>8.3f}  {note}{mark}")
        if r["risks"]:
            flagged.append((i, r))

    print("=" * 92)
    print(f"Total declared duration: {cumulative:.2f}s ({int(cumulative // 3600)}h{int(cumulative % 3600 // 60)}m)")
    print(f"Distinct start_time values: {sorted(starts)}")
    print(f"Distinct video timebases  : {sorted(timebases)}")

    if len(starts) > 1 or len(timebases) > 1:
        print("\n[!] Mixed start_time / timebase -> the batch is NOT safe for plain lossless copy.")
        print("    Splitify now detects this automatically and falls back to Selective Auto-Fix.")
    if flagged:
        print(f"\n[!] {len(flagged)} file(s) look risky (first 10 shown):")
        for i, r in flagged[:10]:
            print(f"    #{i} {r['path']}  -> {'; '.join(r['risks'])}")
        print("\n    Re-mux a risky file to fix it instantly (lossless, seconds):")
        if flagged:
            print(f'      ffmpeg -y -i "{flagged[0][1]["path"]}" -c copy -fflags +genpts '
                  f'-avoid_negative_ts make_zero "{os.path.splitext(flagged[0][1]["path"])[0]}_fixed.mp4"')
    else:
        print("\n[OK] No timestamp anomalies detected - a plain lossless copy should work.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
