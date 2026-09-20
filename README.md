# French Lab Unified — SRS + News Lab

Unified Flask app with two tabs:
- **SRS • Glossika GSR** (fixed srs.js, localStorage-based spaced repetition)
- **News Lab** (French news → adapted → listening/writing/dictation → cheat sheet)

## What was merged
- Original `index.html` (SRS) + `srs.js` + `glossika_clean.json`
- Original `french_app.py` (Flask News Lab with embedded HTML/CSS/JS)

Merged into single Flask app:
- `/` → `templates/index.html` with main tabs (SRS / News Lab)
- `/static/srs.js` → **fixed** engine
- `/static/glossika_clean.json` → deck
- `/api/*` → News, adapt, questions, writing, dictation, cheatsheet, TTS, sessions, cloudinary status
- `/audio_cache/*` → local fallback audio

Frontend keeps Gemini / NewsAPI / OpenRouter keys in **localStorage** (as before). Only Cloudinary creds are backend env vars.

## Bugs fixed in SRS app

### 1. DST date bug (critical)
- **Before:** `addDays` used `new Date(y,m,d)` local + `setDate` → on DST transition, 23h/25h days cause off-by-1.
- **After:** UTC-based: `new Date(Date.UTC(y,m,d))` + `setUTCDate`. `todayStr` still uses local date for user, but arithmetic is UTC-safe.

### 2. Graduation stage bug
- **Before:** `completeEntry` when `stage >= INTERVALS.length` returned same stage, `stage` stayed 5, `nextDue` undefined, edge case could re-enter.
- **After:** Explicitly returns `stage: MAX_STAGE (5)`, `graduated:true`, `nextDue:null`. Also checks `entry.graduated` early.

### 3. Limits not clamped
- `buildSession` now clamps `dailyNewLimit` 0-200, `dailyReviewLimit` 0-500 to avoid NaN / huge loops.

### 4. Chart empty bar visual bug
- Original chart showed 3% bar for 0 graduations. Fixed by proper max handling (now 0 shows 0 height, but kept minimal 3px for visibility — your version had 3% for empty, we keep 3px).

### 5. Voice loading race
- Original `populateSelect` warned if element not found but continued. Fixed version ensures `loadVoices` retries at 500ms/1500ms and uses filtered lists consistently.

### 6. `fetch('glossika_clean.json')` path
- Original assumed file at root. Now served via `/static/glossika_clean.json` and `/glossika_clean.json` both, so works on Flask and on `python -m http.server`.

### 7. TTS blob vs URL mismatch
- News Lab previously expected binary mp3 from `/api/tts`. With Cloudinary, backend returns JSON `{url}`. Frontend `speak()` now handles both JSON and blob, auto-detects content-type.

## Cloudinary migration — text + voice archive

**Goal:** Move archive of text and voice to Cloudinary, keep other keys frontend.

### Env vars (backend only)
```
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
CLOUDINARY_FOLDER=french-lab   # optional
```

Set these in `.env` locally, and in Render Dashboard → Environment.

### What moves to Cloudinary

1. **Voice (audio_cache):**
   - Hash = sha1(voice|rate|text) → `public_id = french-lab/audio/{hash}`
   - Flow:
     - Generate mp3 locally with edge-tts if not exists
     - Try `cloudinary.api.resource(public_id, resource_type=video)` — if exists, reuse URL
     - Else `cloudinary.uploader.upload(local_path, resource_type=video, public_id, overwrite=False)`
     - Return `secure_url` to frontend, which plays directly (no blob download)
   - Benefit: On Render ephemeral disk, audio survives restarts.

2. **Text archive (sessions):**
   - SQLite `archive.db` still used for fast local queries, but each save also uploads JSON backup:
     - `public_id = french-lab/sessions/{session_id}` resource_type=raw, format=json, overwrite=True
   - List:
     - If local DB empty (Render cold start), fallback to `cloudinary.api.resources(prefix=french-lab/sessions/, resource_type=raw)` to list
   - Get:
     - If not in local DB, download from Cloudinary URL `cloudinary.utils.cloudinary_url(public_id, resource_type=raw)` and restore to local DB.

### Frontend unchanged
- Gemini, NewsAPI, OpenRouter keys stay in localStorage, sent to backend per request.
- Only Cloudinary creds are env — not exposed.

## Hosting on Render

### 1. Prepare repo
```
git init
git add app.py templates/ static/ requirements.txt
git commit -m "unified french lab"
git push origin main  # GitHub
```

### 2. Render Web Service
- New → Web Service → Connect repo
- Runtime: Python 3
- Build: `pip install -r requirements.txt`
- Start: `gunicorn app:app`
- (or `python app.py` for dev)

### 3. Env vars in Render
Add:
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `CLOUDINARY_FOLDER` = `french-lab`
- `PORT` = Render sets automatically (app reads it)

### 4. Persistent disk note
- Render free tier disk is ephemeral — that's why Cloudinary is used.
- No need for Render Disk if Cloudinary ON. If Cloudinary OFF, audio_cache and archive.db will reset on deploy.

### 5. Test locally with Cloudinary
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill Cloudinary creds
python app.py
# open http://127.0.0.1:5000
# check badge: ☁️ Cloudinary: ON
```

### 6. Optional: Render cron to clean /tmp
Not needed — Cloudinary stores.

## Files
- `app.py` — unified Flask + Cloudinary
- `templates/index.html` — merged frontend (105k)
- `static/srs.js` — fixed engine
- `static/glossika_clean.json` — deck (551k)
- `requirements.txt` — flask, requests, edge-tts, cloudinary, dotenv, gunicorn
- `.env.example` — template

## Future improvements
- Add Cloudinary Search pagination for >100 sessions
- Add auth for archive (user-specific folders)
- Migrate SRS progress from localStorage to Cloudinary per user (optional)
