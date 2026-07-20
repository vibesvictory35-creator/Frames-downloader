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

Key fix in this version: the direct googlevideo stream URL yt-dlp returns
is tied to the HTTP headers (mainly User-Agent) used when it was issued.
Passing the bare URL to ffmpeg with no headers works on some videos/edge
servers and 403s on others - inconsistent and unpredictable. Now the
headers yt-dlp used are captured and passed to every ffmpeg call that
touches the stream URL, removing that inconsistency entirely.
"""

import base64
import glob
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
import yt_dlp

# --- Cookie support -----------------------------------------------------
# YouTube blocks most datacenter IPs (Railway, AWS, etc.) with a
# "Sign in to confirm you're not a bot" error unless requests carry a real
# logged-in session's cookies. We read those from an environment variable
# (never committed to the repo) so your account session isn't exposed on
# GitHub. Set YOUTUBE_COOKIES_B64 in Railway's Variables tab - see README
# for how to generate it.
COOKIE_FILE_PATH = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")


def setup_cookie_file() -> Optional[str]:
    b64_cookies = os.environ.get("YOUTUBE_COOKIES_B64")
    if not b64_cookies:
        return None
    try:
        raw = base64.b64decode(b64_cookies)
        with open(COOKIE_FILE_PATH, "wb") as f:
            f.write(raw)
        return COOKIE_FILE_PATH
    except Exception:
        return None


COOKIE_FILE = setup_cookie_file()
# -------------------------------------------------------------------------

app = FastAPI(title="Chief's Frame Grabber")

OUTPUT_DIR = tempfile.gettempdir()
POST_TRANSITION_OFFSET = 0.4  # seconds to wait after a detected change before grabbing the frame
CLUSTER_GAP = 0.75            # change-points closer together than this are treated as one cut
FRAME_MAX_AGE_SECONDS = 3600  # sweep frame files older than this on each request


class ProcessRequest(BaseModel):
    url: str
    frame_count: int = 10
    quality: str = "720p"  # best | 1080p | 720p


def _cleanup_old_frames():
    """Deletes previously extracted frame_*.jpg files older than FRAME_MAX_AGE_SECONDS
    so temp storage doesn't grow unbounded across requests."""
    now = time.time()
    for path in glob.glob(os.path.join(OUTPUT_DIR, "frame_*.jpg")):
        try:
            if now - os.path.getmtime(path) > FRAME_MAX_AGE_SECONDS:
                os.remove(path)
        except OSError:
            pass


def _try_extract(youtube_url: str, ydl_opts: dict):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(youtube_url, download=False)


def get_video_data(youtube_url: str, quality: str) -> Tuple[dict, str, Dict[str, str]]:
    """
    One yt-dlp call gets both the full metadata dict AND the direct stream URL
    needed for ffmpeg - no need to hit YouTube twice.

    Also returns the HTTP headers yt-dlp used to obtain that URL. YouTube's
    CDN validates those headers (mainly User-Agent) on some edge servers -
    without them, ffmpeg gets a 403 on some videos even though the URL
    itself is valid. Reusing the same headers on every ffmpeg call fixes
    this reliably.
    """
    format_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "1080p": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080]",
        "720p": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720]",
    }
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": format_map.get(quality, format_map["720p"]),
        "noplaylist": True,
    }
    if COOKIE_FILE:
        base_opts["cookiefile"] = COOKIE_FILE

    if COOKIE_FILE:
        client_order = [["web"], ["android"], ["ios"]]
    else:
        client_order = [["android"], ["ios"], ["web"]]

    errors = []
    info = None
    for player_client in client_order:
        opts = dict(base_opts)
        opts["extractor_args"] = {"youtube": {"player_client": player_client}}
        try:
            info = _try_extract(youtube_url, opts)
            break
        except Exception as e:
            errors.append(f"{player_client[0]}: {e}")
            continue

    if info is None:
        raise RuntimeError("All client attempts failed -> " + " | ".join(errors))

    if "requested_formats" in info and info["requested_formats"]:
        fmt = info["requested_formats"][0]
        stream_url = fmt["url"]
        headers = fmt.get("http_headers") or info.get("http_headers") or {}
    else:
        stream_url = info["url"]
        headers = info.get("http_headers") or {}

    return info, stream_url, headers


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


def _headers_arg(headers: Dict[str, str]) -> str:
    """Formats a headers dict for ffmpeg's -headers flag (each line needs \\r\\n)."""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def find_change_points(stream_url: str, duration: Optional[float], headers: Dict[str, str]) -> List[float]:
    """
    Scans the video once with a low scene-change threshold to catch every
    shot change (not just big hard cuts), returns raw candidate timestamps.
    Low threshold on purpose - clustering afterward removes the noise.
    """
    cmd = ["ffmpeg"]
    if headers:
        cmd += ["-headers", _headers_arg(headers)]
    cmd += [
        "-i", stream_url,
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


def extract_frame(stream_url: str, timestamp_seconds: float, out_path: str, headers: Dict[str, str]):
    cmd = ["ffmpeg", "-y"]
    if headers:
        cmd += ["-headers", _headers_arg(headers)]
    cmd += [
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

    _cleanup_old_frames()

    try:
        info, stream_url, stream_headers = get_video_data(req.url, req.quality)
    except Exception as e:
        raise HTTPException(400, f"Could not resolve video: {e}")

    metadata = build_metadata(info)
    duration = info.get("duration")

    try:
        raw_changes = find_change_points(stream_url, duration, stream_headers)
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
            extract_frame(stream_url, secs, out_path, stream_headers)
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


ALLOWED_THUMBNAIL_HOSTS = ("ytimg.com", "googleusercontent.com", "ggpht.com")


@app.get("/api/download-thumbnail")
def download_thumbnail(url: str):
    """
    Proxies the thumbnail download server-side. Browsers block forcing a
    download on cross-origin images from a plain <a download> link unless
    the remote server cooperates, so we fetch it here and stream it back
    with a proper attachment header instead.
    """
    parsed_host = urllib.parse.urlparse(url).hostname or ""
    if not any(parsed_host.endswith(h) for h in ALLOWED_THUMBNAIL_HOSTS):
        raise HTTPException(400, "URL not from an allowed thumbnail host")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        raise HTTPException(502, f"Could not fetch thumbnail: {e}")

    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": 'attachment; filename="thumbnail.jpg"'},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(os.path.dirname(__file__), "static", "index.html")) as f:
        return f.read()
