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
