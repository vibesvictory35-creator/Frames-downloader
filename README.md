# Chief's Frame Grabber

Paste a YouTube link, get metadata (title, description, tags, category,
thumbnail, views, likes, comments) plus N clear frames picked automatically
from real scene changes in the video.

## IMPORTANT: YouTube bot-check on cloud servers

YouTube blocks most datacenter IPs (Railway, AWS, etc.) with a
"Sign in to confirm you're not a bot" error. This is a server-vs-server
issue, not a bug in this tool - it happens to almost every self-hosted
YouTube tool eventually. The fix is to give yt-dlp cookies from a real,
logged-in YouTube session.

### How to set this up (one-time, ~5 minutes)

1. On your computer, log into YouTube in Chrome (any Google account works -
   doesn't need to be a special account, just logged in).
2. Install the "Get cookies.txt LOCALLY" extension from the Chrome Web Store.
3. Go to youtube.com, click the extension icon, export cookies for
   youtube.com - this downloads a `cookies.txt` file.
4. Convert that file to base64 (so it can safely go in an environment
   variable instead of your public GitHub repo):
   - Mac/Linux terminal: `base64 -i cookies.txt | tr -d '\n' > cookies_b64.txt`
   - Or use any online "file to base64" converter (paste the file content).
5. Copy the resulting base64 text (it'll be one long line).
6. In Railway: your service → **Variables** tab → **New Variable**
   - Name: `YOUTUBE_COOKIES_B64`
   - Value: paste the base64 text
7. Save - Railway will redeploy automatically.

**Never commit cookies.txt to GitHub directly** - that file contains your
real YouTube session and anyone with repo access could use it to access
your account. The base64 + Railway Variable approach keeps it private.

Cookies do expire eventually (typically weeks to a couple months) - if the
bot-check error comes back later, just re-export and update the Railway
variable.

## How it works

1. `yt-dlp` resolves the direct video stream URL + full metadata in one
   call, trying Android/iOS client spoofing first, then falling back to
   cookies if you've set them up.
2. The video is scanned once for scene changes (low threshold, catches
   everything including crossfades).
3. Nearby detection points are clustered together (a single cut/crossfade
   fires the detector across several frames) and a frame is grabbed
   ~0.4 seconds after the cluster ends - once the new shot has visually
   settled, avoiding the blurry mid-transition frame.
4. If there are more detected changes than frames you asked for, picks are
   spread evenly across the timeline.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Needs `ffmpeg` installed on the system.

## Deploy

Push to GitHub, connect the repo in Railway - it auto-detects the
`Dockerfile` (ffmpeg is baked in). Set `YOUTUBE_COOKIES_B64` in Variables
as described above.

## API

`POST /api/process`
```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "frame_count": 10,
  "quality": "720p"
}
```
Returns `{ metadata: {...}, frames: [...], detected_changes: N }`.

`GET /api/frame/{filename}` - returns the JPEG for a previously extracted frame.

## Known limitations

- No cleanup job for extracted frames yet - add a periodic sweep to delete
  files older than ~1 hour, or temp storage grows.
- No rate limiting.
- Captions burned into the video for its entire runtime can't be avoided by
  frame timing alone - that would need OCR-based caption detection, which
  is a heavier separate feature.
- Cookies expire periodically and need re-exporting.
