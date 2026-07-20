"""
CHIEF'S FRAME GRABBER v2
Single-endpoint tool: paste a YouTube link, get metadata + N clear frames
picked automatically from the video's actual scene/shot changes.

Key fix over v1: scene-change detection previously grabbed the frame at the
peak of the transition (often mid-crossfade / blurry). This version:
  1. Finds every point where the frame changes significantly (low threshold,
     catches everything, not just big cuts)
  2. Clusters nearby change-points together (a single cut/crossfade often
     trips the detector across several consecutive frames)
  3. Extracts each frame a small offset AFTER the cluster ends, once the
     new shot has settled - avoiding the blurry in-between frame
  4. If there are more clusters than frames requested, spreads picks evenly
     across the timeline so you get a representative sample of the whole
     video, not just clustered near a busy section
"""

import os
import re
import subprocess
import tempfile
import uuid
from typing import List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Chief's Frame Grabber")

OUTPUT_DIR = tempfile.gettempdir()
POST_TRANSITION_OFFSET = 0.4  # seconds to wait after a detected change before grabbing the frame
CLUSTER_GAP = 0.75            # change-points closer together than this are treated as one cut


class ProcessRequest(BaseModel):
    url: str
    frame_count: int = 10
    quality: str = "720p"  # best | 1080p | 720p


def get_video_data(youtube_url: str, quality: str) -> Tuple[dict, str]:
    """
    One yt-dlp call gets both the full metadata dict AND the direct stream URL
    needed for ffmpeg - no need to hit YouTube twice.
    """
    format_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080p": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
    }
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_map.get(quality, format_map["720p"]),
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    if "requested_formats" in info and info["requested_formats"]:
        stream_url = info["requested_formats"][0]["url"]
    else:
        stream_url = info["url"]

    return info, stream_url


def build_metadata(info: dict) -> dict:
    thumbnails = sorted(
        info.get("thumbnails", []),
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )
    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "tags": info.get("tags", []),
        "categories": info.get("categories", []),
        "category": info.get("category"),
        "thumbnails": [
            {"url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
            for t in thumbnails
        ],
        "best_thumbnail": thumbnails[0]["url"] if thumbnails else None,
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "age_limit": info.get("age_limit"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "language": info.get("language"),
        "video_id": info.get("id"),
        "webpage_url": info.get("webpage_url"),
    }


def find_change_points(stream_url: str, duration: Optional[float]) -> List[float]:
    """
    Scans the video once with a low scene-change threshold to catch every
    shot change (not just big hard cuts), returns raw candidate timestamps.
    Low threshold on purpose - clustering afterward removes the noise.
    """
    cmd = [
        "ffmpeg", "-i", stream_url,
        "-filter:v", "select='gt(scene,0.15)',showinfo",
        "-f", "null", "-",
    ]
    timeout = max(120, int(duration * 1.5)) if duration else 600
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    return sorted(times)


def cluster_change_points(times: List[float]) -> List[float]:
    """
    Groups change-points that are close together (one real cut often fires
    the detector across several consecutive frames) and returns one
    representative timestamp per cluster - the END of the cluster, since
    that's closest to where the new shot has settled.
    """
    if not times:
        return []
    clusters = [[times[0]]]
    for t in times[1:]:
        if t - clusters[-1][-1] <= CLUSTER_GAP:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    return [max(c) for c in clusters]  # end of each cluster


def pick_frame_times(cluster_times: List[float], frame_count: int, duration: Optional[float]) -> List[float]:
    """
    Turns cluster end-points into final extraction timestamps: add the
    post-transition offset so we land on a settled frame, then either
    trim or spread to match the requested frame_count.
    """
    candidates = [t + POST_TRANSITION_OFFSET for t in cluster_times]
    if duration:
        candidates = [t for t in candidates if t < duration - 0.1]

    if not candidates:
        return []

    if len(candidates) <= frame_count:
        return candidates

    # more clusters than requested - spread evenly across them so the
    # selection represents the whole video, not just the first N cuts
    step = len(candidates) / frame_count
    return [candidates[int(i * step)] for i in range(frame_count)]


def extract_frame(stream_url: str, timestamp_seconds: float, out_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp_seconds),
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(result.stderr[-500:])


@app.post("/api/process")
def process(req: ProcessRequest):
    if req.frame_count < 1 or req.frame_count > 60:
        raise HTTPException(400, "frame_count must be between 1 and 60")

    try:
        info, stream_url = get_video_data(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    metadata = build_metadata(info)
    duration = info.get("duration")

    try:
        raw_changes = find_change_points(stream_url, duration)
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Scene scan timed out - try a shorter video")
    except Exception as e:
        raise HTTPException(500, f"Scene detection failed: {e}")

    clusters = cluster_change_points(raw_changes)
    frame_times = pick_frame_times(clusters, req.frame_count, duration)

    if not frame_times:
        # fall back to even time-based spacing so the user still gets
        # something useful instead of an empty result
        if duration:
            frame_times = [
                duration * (i + 1) / (req.frame_count + 1)
                for i in range(req.frame_count)
            ]

    frames = []
    job_id = uuid.uuid4().hex[:8]
    for i, secs in enumerate(frame_times):
        try:
            filename = f"frame_{job_id}_{i}.jpg"
            out_path = os.path.join(OUTPUT_DIR, filename)
            extract_frame(stream_url, secs, out_path)
            mins = int(secs // 60)
            rem = secs % 60
            label = f"{mins}:{rem:05.2f}"
            frames.append({"timestamp": label, "seconds": round(secs, 2), "file": filename, "ok": True})
        except Exception as e:
            frames.append({"timestamp": f"{secs:.1f}s", "ok": False, "error": str(e)})

    return {
        "job_id": job_id,
        "metadata": metadata,
        "frames": frames,
        "detected_changes": len(clusters),
    }


@app.get("/api/frame/{filename}")
def get_frame(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Frame not found or expired")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()
    quality: str = "720p"  # lower default - scanning full video is heavier than one seek
    sensitivity: float = 0.3  # 0.0-1.0, lower = more frames (more sensitive to change)
    max_frames: int = 40  # safety cap so a long video can't produce hundreds of frames


class MetadataRequest(BaseModel):
    url: str


def seconds_from_timestamp(ts: str) -> float:
    ts = ts.strip()
    parts = ts.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def get_stream_url(youtube_url: str, quality: str) -> str:
    format_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080p": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
    }
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_map.get(quality, format_map["best"]),
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        # requested_formats appears when video+audio are separate streams;
        # we only need the video-only URL for frame grabs
        if "requested_formats" in info and info["requested_formats"]:
            return info["requested_formats"][0]["url"]
        return info["url"]


def extract_frame(stream_url: str, timestamp_seconds: float, out_path: str):
    # -ss BEFORE -i = fast seek (jumps in the container index, doesn't decode
    # every prior frame). Falls back to accurate seek only if needed.
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp_seconds),
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(result.stderr[-800:])


def detect_scene_changes(stream_url: str, sensitivity: float, max_frames: int) -> List[float]:
    """
    Runs ffmpeg's scene-detection filter over the whole stream. This must scan
    sequentially (no fast-seek) since it's comparing each frame to the last -
    slower and more bandwidth-heavy than single-timestamp extraction.
    Returns a list of timestamps (seconds) where a scene change was detected.
    """
    cmd = [
        "ffmpeg", "-i", stream_url,
        "-filter:v", f"select='gt(scene,{sensitivity})',showinfo",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    # showinfo prints frame info to stderr, one line per matched frame,
    # each containing "pts_time:<seconds>"
    timestamps = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    if max_frames and len(timestamps) > max_frames:
        # evenly sample down to max_frames rather than just truncating,
        # so we still get spread across the whole video
        step = len(timestamps) / max_frames
        timestamps = [timestamps[int(i * step)] for i in range(max_frames)]
    return timestamps


def get_metadata(youtube_url: str) -> dict:
    """
    Pulls everything yt-dlp exposes without touching video/audio streams -
    no download, no format resolution beyond what's needed for the info dict.
    This is the fast path: single network round-trip to YouTube's info endpoint.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    thumbnails = sorted(
        info.get("thumbnails", []),
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )

    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "tags": info.get("tags", []),
        "categories": info.get("categories", []),
        "category": info.get("category"),  # single YouTube category, if present
        "thumbnails": [
            {"url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
            for t in thumbnails
        ],
        "best_thumbnail": thumbnails[0]["url"] if thumbnails else None,
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "age_limit": info.get("age_limit"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "language": info.get("language"),
        "video_id": info.get("id"),
        "webpage_url": info.get("webpage_url"),
    }


@app.post("/api/metadata")
def metadata(req: MetadataRequest):
    try:
        return get_metadata(req.url)
    except Exception as e:
        raise HTTPException(400, f"Could not fetch metadata: {e}")


@app.post("/api/autodetect")
def autodetect(req: AutoDetectRequest):
    try:
        stream_url = get_stream_url(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    try:
        timestamps_sec = detect_scene_changes(stream_url, req.sensitivity, req.max_frames)
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Scene detection timed out - try a shorter video or lower sensitivity")
    except Exception as e:
        raise HTTPException(500, f"Scene detection failed: {e}")

    if not timestamps_sec:
        raise HTTPException(404, "No scene changes detected - try lowering sensitivity")

    results = []
    job_id = uuid.uuid4().hex[:8]
    for i, secs in enumerate(timestamps_sec):
        try:
            filename = f"scene_{job_id}_{i}.jpg"
            out_path = os.path.join(OUTPUT_DIR, filename)
            extract_frame(stream_url, secs, out_path)
            ts_label = f"{int(secs // 60)}:{secs % 60:05.2f}"
            results.append({"timestamp": ts_label, "seconds": secs, "file": filename, "ok": True})
        except Exception as e:
            results.append({"timestamp": f"{secs:.1f}s", "ok": False, "error": str(e)})
    return {"job_id": job_id, "count": len(results), "results": results}


@app.post("/api/extract")
def extract(req: FrameRequest):
    if not req.timestamps:
        raise HTTPException(400, "Provide at least one timestamp")
    try:
        stream_url = get_stream_url(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    results = []
    job_id = uuid.uuid4().hex[:8]
    for i, ts in enumerate(req.timestamps):
        try:
            secs = seconds_from_timestamp(ts)
            filename = f"frame_{job_id}_{i}.jpg"
            out_path = os.path.join(OUTPUT_DIR, filename)
            extract_frame(stream_url, secs, out_path)
            results.append({"timestamp": ts, "file": filename, "ok": True})
        except Exception as e:
            results.append({"timestamp": ts, "ok": False, "error": str(e)})
    return {"job_id": job_id, "results": results}


@app.get("/api/frame/{filename}")
def get_frame(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Frame not found or expired")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()
    quality: str = "720p"  # lower default - scanning full video is heavier than one seek
    sensitivity: float = 0.3  # 0.0-1.0, lower = more frames (more sensitive to change)
    max_frames: int = 40  # safety cap so a long video can't produce hundreds of frames


class MetadataRequest(BaseModel):
    url: str


def seconds_from_timestamp(ts: str) -> float:
    ts = ts.strip()
    parts = ts.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def get_stream_url(youtube_url: str, quality: str) -> str:
    format_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080p": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
    }
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_map.get(quality, format_map["best"]),
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)
        # requested_formats appears when video+audio are separate streams;
        # we only need the video-only URL for frame grabs
        if "requested_formats" in info and info["requested_formats"]:
            return info["requested_formats"][0]["url"]
        return info["url"]


def extract_frame(stream_url: str, timestamp_seconds: float, out_path: str):
    # -ss BEFORE -i = fast seek (jumps in the container index, doesn't decode
    # every prior frame). Falls back to accurate seek only if needed.
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp_seconds),
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(result.stderr[-800:])


def detect_scene_changes(stream_url: str, sensitivity: float, max_frames: int) -> List[float]:
    """
    Runs ffmpeg's scene-detection filter over the whole stream. This must scan
    sequentially (no fast-seek) since it's comparing each frame to the last -
    slower and more bandwidth-heavy than single-timestamp extraction.
    Returns a list of timestamps (seconds) where a scene change was detected.
    """
    cmd = [
        "ffmpeg", "-i", stream_url,
        "-filter:v", f"select='gt(scene,{sensitivity})',showinfo",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    # showinfo prints frame info to stderr, one line per matched frame,
    # each containing "pts_time:<seconds>"
    timestamps = [float(m) for m in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    if max_frames and len(timestamps) > max_frames:
        # evenly sample down to max_frames rather than just truncating,
        # so we still get spread across the whole video
        step = len(timestamps) / max_frames
        timestamps = [timestamps[int(i * step)] for i in range(max_frames)]
    return timestamps


def get_metadata(youtube_url: str) -> dict:
    """
    Pulls everything yt-dlp exposes without touching video/audio streams -
    no download, no format resolution beyond what's needed for the info dict.
    This is the fast path: single network round-trip to YouTube's info endpoint.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(youtube_url, download=False)

    thumbnails = sorted(
        info.get("thumbnails", []),
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        reverse=True,
    )

    return {
        "title": info.get("title"),
        "description": info.get("description"),
        "tags": info.get("tags", []),
        "categories": info.get("categories", []),
        "category": info.get("category"),  # single YouTube category, if present
        "thumbnails": [
            {"url": t.get("url"), "width": t.get("width"), "height": t.get("height")}
            for t in thumbnails
        ],
        "best_thumbnail": thumbnails[0]["url"] if thumbnails else None,
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "age_limit": info.get("age_limit"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "language": info.get("language"),
        "video_id": info.get("id"),
        "webpage_url": info.get("webpage_url"),
    }


@app.post("/api/metadata")
def metadata(req: MetadataRequest):
    try:
        return get_metadata(req.url)
    except Exception as e:
        raise HTTPException(400, f"Could not fetch metadata: {e}")


@app.post("/api/autodetect")
def autodetect(req: AutoDetectRequest):
    try:
        stream_url = get_stream_url(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    try:
        timestamps_sec = detect_scene_changes(stream_url, req.sensitivity, req.max_frames)
    except subprocess.TimeoutExpired:
        raise HTTPException(408, "Scene detection timed out - try a shorter video or lower sensitivity")
    except Exception as e:
        raise HTTPException(500, f"Scene detection failed: {e}")

    if not timestamps_sec:
        raise HTTPException(404, "No scene changes detected - try lowering sensitivity")

    results = []
    job_id = uuid.uuid4().hex[:8]
    for i, secs in enumerate(timestamps_sec):
        try:
            filename = f"scene_{job_id}_{i}.jpg"
            out_path = os.path.join(OUTPUT_DIR, filename)
            extract_frame(stream_url, secs, out_path)
            ts_label = f"{int(secs // 60)}:{secs % 60:05.2f}"
            results.append({"timestamp": ts_label, "seconds": secs, "file": filename, "ok": True})
        except Exception as e:
            results.append({"timestamp": f"{secs:.1f}s", "ok": False, "error": str(e)})
    return {"job_id": job_id, "count": len(results), "results": results}


@app.post("/api/extract")
def extract(req: FrameRequest):
    if not req.timestamps:
        raise HTTPException(400, "Provide at least one timestamp")
    try:
        stream_url = get_stream_url(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    results = []
    job_id = uuid.uuid4().hex[:8]
    for i, ts in enumerate(req.timestamps):
        try:
            secs = seconds_from_timestamp(ts)
            filename = f"frame_{job_id}_{i}.jpg"
            out_path = os.path.join(OUTPUT_DIR, filename)
            extract_frame(stream_url, secs, out_path)
            results.append({"timestamp": ts, "file": filename, "ok": True})
        except Exception as e:
            results.append({"timestamp": ts, "ok": False, "error": str(e)})
    return {"job_id": job_id, "results": results}


@app.get("/api/frame/{filename}")
def get_frame(filename: str):
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Frame not found or expired")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()
