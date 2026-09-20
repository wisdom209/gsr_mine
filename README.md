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

## Bug-hunt fixes (round 2)

**Security**
- `app.run(host="0.0.0.0", debug=True)` exposed the Werkzeug debugger (remote code execution) to the network. Now binds `127.0.0.1`, debug is opt-in (`FLASK_DEBUG=1`) and is refused on non-local hosts.
- Session ids from the client were used in a `/tmp/<id>.json` path and in Cloudinary public_ids (path traversal / id injection). Ids are now validated (`[A-Za-z0-9_-]{1,64}`) and the backup is uploaded from memory.
- Gemini key was sent in the URL query string, so network errors echoed it back to the browser. Now sent as `x-goog-api-key` header and scrubbed from error text.

**Data / backend**
- Cloudinary session backups: raw assets keep their extension in the public_id, so the restore URL (`…/sid.json`) never matched what was uploaded. The `.json` is now part of the public_id (legacy ids still restored/deleted). `created_at` was also overwritten on each backup.
- TTS cache: a failed/interrupted synthesis left a partial or empty `.mp3` that was then served forever. Audio is written to a temp file and moved into place atomically; empty cache files are regenerated. Text length / voice / rate are validated.
- AI output is validated: `answer_index` coerced to int 0-3, grader `score`/`index` coerced to numbers (string scores produced `Average: 4030%`), empty quizzes/adaptations return a clear error instead of a blank screen, unknown CEFR level no longer crashes with `KeyError`, JSON-list request bodies no longer 500.
- NewsAPI 429 no longer burns 5 requests.

**SRS**
- `0 new/day` still gave 10 new cards (`x || default`). Fixed in `srs.js` (README fix #3 claimed clamping but 0 was treated as "unset").
- Saved settings were overwritten with defaults on page load (lost on the 2nd reload); saved voice was stored as an index but compared to voice *names*, so it never restored. Voices are now stored by `voiceURI` and applied whenever the (asynchronously loaded) voice list appears.
- Keyboard shortcuts (Space/→/S/R/F/E/H) also fired while the **News Lab** tab was open — → could silently *complete* SRS cards — and reacted to Ctrl+R / Ctrl+F / Ctrl+S. Now gated on the visible tab and ignore modifier keys.
- Pressing Next quickly made the previous sentence keep playing (shared `cancelled` flag reset by the new sequence). Replaced with a play token.
- If `glossika_clean.json` failed to load, `document.body.innerHTML = …` wiped the entire app including the News Lab. Now only the SRS pane shows the error.
- Chunk size ≤ 0 produced an empty session (Start did nothing). Missing `speechSynthesis` no longer kills the script.

**News Lab**
- Saved Gemini/NewsAPI keys were shown in the inputs but never loaded into state, so resuming an archived lesson straight after a page load sent empty keys.
- Choosing a new article kept the previous `adapted` text and unlocked steps (wrong archive title, quizzes generated for the wrong article); re-adapting kept stale questions. Stale score / "Continue" buttons carried over between lessons.
- Rapid saves created duplicate archive entries; a slow save from the previous lesson could overwrite the new lesson's id.
- NewsAPI's `[+1234 chars]` truncation marker was fed to the model.
- Two stray `}` in the merged CSS made the browser drop the `#main-news { background; color; min-height }` rule.
- Switching tabs now stops any audio that is playing.

Not changed (FYI): `app.py` still contains ~1,200 lines of dead embedded `INDEX_HTML/STYLE_CSS/APP_JS` (the `/` route serves `templates/index.html`); `old/` is listed in `.gitignore` but is tracked in git.

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
- Start: `gunicorn app:app --timeout 180` (gunicorn's default 30 s timeout kills workers during slow Gemini / TTS calls)
- (or `python app.py` for local dev — binds 127.0.0.1; set `FLASK_DEBUG=1` for debug mode)

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
