"""
CHIEF'S FRAME GRABBER
HD frame extraction from YouTube videos at exact timestamps.

Approach: yt-dlp resolves a direct stream URL (no full download to disk),
ffmpeg seeks to the timestamp on that stream and pulls a single frame.
This keeps it fast and cheap on a Railway/HF Spaces instance -
only the seek window is buffered, not the whole video.
"""

import os
import re
import subprocess
import tempfile
import uuid
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Chief's Frame Grabber")

OUTPUT_DIR = tempfile.gettempdir()


class FrameRequest(BaseModel):
    url: str
    timestamps: List[str]  # e.g. ["00:01:23", "1:45", "90"]
    quality: str = "best"  # best | 1080p | 720p


class AutoDetectRequest(BaseModel):
    url: str
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
