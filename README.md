# Chief's Frame Grabber

HD frame extraction from YouTube videos at exact timestamps — no full video download.

## How it works

1. `yt-dlp` resolves the direct video stream URL for the given YouTube link (doesn't download the file).
2. `ffmpeg` seeks to each requested timestamp on that stream and pulls a single HD frame.
3. Frames are served back to the browser and are downloadable.

This is lighter than a full download+extract pipeline, but each timestamp still requires ffmpeg
to open the stream and seek, so response time scales with the number of timestamps requested
(expect 2-6s per frame depending on video quality and seek accuracy).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Needs `ffmpeg` installed on the system (`apt install ffmpeg` / already in your ComfyUI pipeline environment).

## Deploy

### Railway
Push this folder as a repo, Railway will detect the `Dockerfile` automatically (ffmpeg is baked in).

### Hugging Face Spaces (Docker SDK)
Same Dockerfile works — set the Space SDK to "Docker" and it'll run on port 7860.

## API

`POST /api/extract`
```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "timestamps": ["1:23", "00:04:10", "90"],
  "quality": "best"
}
```

`GET /api/frame/{filename}` — returns the JPEG for a previously extracted frame.

## Known limitations / things to harden before public launch

- Frames are written to the OS temp dir with no cleanup job yet — add a periodic sweep
  (cron or background task) to delete files older than ~1 hour, or storage will grow.
- No rate limiting — same concern as your other public tools (Fetcher/Stitcher); worth
  putting behind the same reverse-proxy rate limits you already use.
- Very long or live videos may need a longer ffmpeg timeout than the current 60s.
- Some age-restricted or region-locked videos will fail at the yt-dlp resolve step —
  surface that error to the user rather than a generic 500.
- Fast seek (`-ss` before `-i`) is usually frame-accurate within ~1s on YouTube's
  fragmented streams; if you need frame-perfect accuracy, switch to `-ss` after `-i`
  (slower — decodes from start of stream to the timestamp).
