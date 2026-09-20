"""
French Lab Unified — SRS + News Lab + Cloudinary
"""
import os, io, json, re, uuid, asyncio, hashlib, sqlite3
from contextlib import closing
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(BASE_DIR, "audio_cache")
DB_PATH = os.path.join(BASE_DIR, "archive.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATES_DIR)

USE_CLOUDINARY = bool(os.getenv("CLOUDINARY_CLOUD_NAME") and os.getenv("CLOUDINARY_API_KEY") and os.getenv("CLOUDINARY_API_SECRET"))
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "french-lab")

cloudinary_uploader = None
cloudinary_api = None
cloudinary_utils = None

if USE_CLOUDINARY:
    try:
        import cloudinary
        import cloudinary.uploader
        import cloudinary.api
        import cloudinary.utils
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True
        )
        cloudinary_uploader = cloudinary.uploader
        cloudinary_api = cloudinary.api
        cloudinary_utils = cloudinary.utils
        print(f"Cloudinary enabled — folder: {CLOUDINARY_FOLDER}")
    except Exception as e:
        print(f"Cloudinary failed: {e}")
        USE_CLOUDINARY = False
else:
    print("Cloudinary disabled — local mode")

MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
# OpenRouter (Qwen) fallback models, tried in order if Gemini fails (or if no Gemini key was given).
OPENROUTER_MODELS = ["qwen/qwen3-8b:free", "qwen/qwen3-4b:free", "qwen/qwen3-next-80b-a3b-instruct:free"]


# ============================================================ Archive (sqlite)
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            title TEXT,
            source TEXT,
            level TEXT,
            last_step TEXT,
            data TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()

LEVEL_GUIDE = {
    "A1": "présent de l'indicatif uniquement, phrases de 5 à 8 mots, vocabulaire très fréquent, "
          "pas de subjonctif, pas de passé simple, pas de pronoms relatifs complexes. 110-150 mots.",
    "A2": "présent, passé composé, futur proche, imparfait simple. Phrases de 8 à 12 mots. "
          "Connecteurs simples (et, mais, parce que, alors). 150-200 mots.",
    "B1": "tous les temps courants, y compris imparfait/passé composé alternés, conditionnel, "
          "pronoms relatifs (qui, que, dont, où). Phrases de 12 à 18 mots. 200-260 mots.",
    "B2": "langue riche et nuancée, subjonctif, voix passive, connecteurs logiques variés, "
          "expressions idiomatiques. 240-320 mots.",
}


# ============================================================ Gemini helpers
def _loads(txt: str):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt)
    out = json.loads(txt)
    if not isinstance(out, dict):
        # Every caller does out.get(...); a bare list/str would crash later with a cryptic error
        raise ValueError("model returned JSON that is not an object")
    return out


def _scrub(msg, *secrets):
    """Never echo API keys back to the browser / logs (network errors embed the request URL)."""
    msg = str(msg)
    for sec in secrets:
        if sec:
            msg = msg.replace(sec, "***")
    return msg


def gemini_json(api_key: str, prompt: str, temperature: float = 0.8):
    last_err = "unknown error"
    for model in MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        try:
            r = requests.post(url, headers={"x-goog-api-key": api_key}, json=payload, timeout=120)
        except Exception as e:
            last_err = _scrub(f"network: {e}", api_key)
            continue
        if r.status_code != 200:
            last_err = _scrub(f"{model} → HTTP {r.status_code}: {r.text[:250]}", api_key)
            continue
        try:
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            txt = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            return _loads(txt)
        except Exception as e:
            last_err = f"{model} → parse error: {e}"
            continue
    raise RuntimeError(last_err)


def openrouter_json(api_key: str, prompt: str, temperature: float = 0.8):
    last_err = "unknown error"
    for model in OPENROUTER_MODELS:
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "response_format": {"type": "json_object"},
                },
                timeout=120,
            )
        except Exception as e:
            last_err = _scrub(f"network: {e}", api_key)
            continue
        if r.status_code != 200:
            last_err = _scrub(f"{model} → HTTP {r.status_code}: {r.text[:250]}", api_key)
            continue
        try:
            data = r.json()
            txt = data["choices"][0]["message"]["content"]
            return _loads(txt)
        except Exception as e:
            last_err = f"{model} → parse error: {e}"
            continue
    raise RuntimeError(last_err)


def ai_json(gemini_key: str, openrouter_key: str, prompt: str, temperature: float = 0.8):
    """Try Gemini first (primary). Fall back to OpenRouter (Qwen, free tier) if Gemini
    fails/is missing and an OpenRouter key was provided. Raises with a combined error
    message if both fail."""
    gemini_key = (gemini_key or "").strip()
    openrouter_key = (openrouter_key or "").strip()
    if not gemini_key and not openrouter_key:
        raise RuntimeError("No Gemini or OpenRouter API key provided.")

    errors = []
    if gemini_key:
        try:
            return gemini_json(gemini_key, prompt, temperature)
        except Exception as e:
            errors.append(f"Gemini failed ({e})")
    if openrouter_key:
        try:
            return openrouter_json(openrouter_key, prompt, temperature)
        except Exception as e:
            errors.append(f"OpenRouter fallback failed ({e})")
    raise RuntimeError(" — then — ".join(errors) or "No usable API key.")


def body():
    d = request.get_json(force=True, silent=True)
    return d if isinstance(d, dict) else {}   # a JSON list/str body used to crash every route with a 500


def norm_level(level):
    """LEVEL_GUIDE[level] raised KeyError (-> cryptic 500) for anything but A1/A2/B1/B2."""
    level = str(level or "").strip().upper()
    return level if level in LEVEL_GUIDE else "A1"


def clean_questions(raw):
    """Keep only well-formed MCQs. answer_index is coerced to int 0-3 (LLMs often return "2"),
    otherwise the frontend's strict === comparison never marks the right answer."""
    out = []
    for q in raw if isinstance(raw, list) else []:
        if not isinstance(q, dict):
            continue
        opts = q.get("options")
        if not q.get("question") or not isinstance(opts, list) or len(opts) != 4:
            continue
        try:
            idx = int(q.get("answer_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx <= 3:
            continue
        out.append({"question": str(q["question"]), "options": [str(o) for o in opts], "answer_index": idx})
    return out


def clean_results(raw, with_errors=False):
    """Grader output -> {index:int, score:0-100 number, ...}. A string score made the frontend
    do `total += "85"` (string concat) and show NaN averages."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            idx = int(r.get("index"))
        except (TypeError, ValueError):
            continue
        try:
            score = max(0.0, min(100.0, float(r.get("score"))))
        except (TypeError, ValueError):
            score = 0.0
        item = {"index": idx, "score": round(score), "corrected": str(r.get("corrected") or ""),
                "feedback": str(r.get("feedback") or "")}
        if with_errors:
            errs = r.get("errors")
            item["errors"] = [str(e) for e in errs] if isinstance(errs, list) else []
        out.append(item)
    return out


def items_list(d):
    raw = d.get("items")
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def no_result(what):
    return jsonify({"error": f"The AI returned no usable {what}. Please try again."}), 502


# ============================================================ Prompts
def p_adapt(text, level, title):
    return f'''Tu es un professeur expérimenté de Français Langue Étrangère (FLE).

Adapte l'article de presse français ci-dessous pour un apprenant de niveau CECRL {level}.
RÈGLES DE NIVEAU ({level}) : {LEVEL_GUIDE[level]}

Consignes :
- Garde les faits et le sens de l'article original.
- Écris un français naturel et correct.
- Ne traduis PAS en anglais. N'ajoute PAS de glossaire ni d'exercices.
- Donne un titre adapté au niveau.

Titre original : {title}
Article original :
"""
{text}
"""

Réponds UNIQUEMENT avec ce JSON valide :
{{"title": "titre adapté en français", "text": "l'article adapté en français"}}
'''


def p_questions(text, level, n=4, variant="first"):
    focus = {
        "first": "l'idée principale, les faits principaux et le vocabulaire en contexte",
        "second": "les détails précis, les relations de cause à effet, les chiffres et les inférences",
    }.get(variant, "l'idée principale")
    return f'''Tu es un professeur de FLE qui prépare un test de compréhension orale.

Voici un article français adapté au niveau CECRL {level} :
"""
{text}
"""

Écris exactement {n} questions à choix multiples EN FRANÇAIS sur cet article, adaptées à un apprenant {level}.
Concentre-toi sur {focus}.
Chaque question doit avoir exactement 4 options et une seule bonne réponse.
Les questions doivent être répondables UNIQUEMENT à partir de l'article.

Réponds UNIQUEMENT avec ce JSON valide :
{{"questions": [{{"question": "…", "options": ["…", "…", "…", "…"], "answer_index": 0}}]}}
'''


def p_writing_a(text, level):
    return f'''Tu es un professeur de FLE.

Article français (niveau {level}) :
"""
{text}
"""

Écris 4 questions ouvertes EN FRANÇAIS sur cet article, pour un apprenant {level}.
L'apprenant devra répondre par des phrases complètes.
Chaque question doit avoir un petit indice ("hint") en français.

Réponds UNIQUEMENT avec ce JSON valide :
{{"questions": [{{"question": "…", "hint": "…"}}]}}
'''


def p_writing_b(text, level):
    return f'''Tu es un professeur de FLE.

Article français (niveau {level}) :
"""
{text}
"""

Extrais 5 structures de phrase ou éléments de vocabulaire utiles de cet article,
adaptés à un apprenant {level}.
Pour chacun donne : la structure, son sens en anglais, et un exemple tiré de l'article.

Réponds UNIQUEMENT avec ce JSON valide :
{{"items": [{{"structure": "…", "meaning": "…", "example": "…"}}]}}
'''


def p_dictation(text, level):
    return f'''Tu es un professeur de FLE.

Article français (niveau {level}) :
"""
{text}
"""

Choisis 4 phrases courtes (6 à 14 mots) tirées ou légèrement adaptées de l'article,
adaptées à une dictée pour un apprenant {level}.
Les phrases doivent être indépendantes et naturelles.

Réponds UNIQUEMENT avec ce JSON valide :
{{"sentences": ["…", "…", "…", "…"]}}
'''


def p_check_writing(items, level):
    lines = []
    for i, it in enumerate(items):
        lines.append(f'{i}. CONSIGNE : {it.get("prompt","")}\n   RÉPONSE : {it.get("answer","")}')
    joined = "\n".join(lines)
    return f'''Tu es un professeur de FLE qui corrige la production écrite d'un apprenant de niveau {level}.

Voici les réponses :
{joined}

Pour CHAQUE réponse :
- corrige la phrase en français correct ("corrected")
- liste les erreurs ("errors"), en anglais, courtes
- donne un retour encourageant en français ("feedback")
- donne une note de 0 à 100 ("score")

Réponds UNIQUEMENT avec ce JSON valide :
{{"results": [{{"index": 0, "corrected": "…", "errors": ["…"], "feedback": "…", "score": 0}}]}}
'''


def p_check_dictation(items, level):
    lines = []
    for i, it in enumerate(items):
        lines.append(f'{i}. ATTENDU : {it.get("expected","")}\n   ÉCRIT : {it.get("answer","")}')
    joined = "\n".join(lines)
    return f'''Tu corriges une dictée de français (niveau {level}).

{joined}

Pour chaque paire, compare l'écrit avec l'attendu.
- Ignore les différences de ponctuation finale.
- Compte les erreurs d'accent, d'orthographe et de grammaire.
- "score" = pourcentage de mots corrects (0-100).
- "feedback" en français, court.
- "corrected" = la phrase attendue.

Réponds UNIQUEMENT avec ce JSON valide :
{{"results": [{{"index": 0, "score": 0, "feedback": "…", "corrected": "…"}}]}}
'''


def p_cheatsheet(text, level, structures, sentences):
    struct_lines = "\n".join(
        f'- {s.get("structure","")} : {s.get("meaning","")}' for s in (structures or [])
    ) or "(aucune)"
    dict_lines = "\n".join(f"- {s}" for s in (sentences or [])) or "(aucune)"
    return f'''Tu es un professeur de FLE qui prépare une fiche de révision ("cheat sheet") de fin de leçon
pour un apprenant de niveau CECRL {level}, à partir de tout ce qui a été travaillé.

Article travaillé (niveau {level}) :
"""
{text}
"""

Structures déjà rencontrées à l'écrit :
{struct_lines}

Phrases travaillées à la dictée :
{dict_lines}

Construis une fiche de révision concise et utile pour réviser rapidement :
1) "vocab" : 8 à 12 mots ou expressions clés de la leçon avec leur traduction en anglais.
2) "structures" : 4 à 6 structures grammaticales importantes, chacune avec un court exemple en français.
3) "tips" : 3 conseils de révision courts et pratiques, en français, adaptés au niveau {level}.

Réponds UNIQUEMENT avec ce JSON valide :
{{"vocab": [{{"word": "…", "meaning": "…"}}], "structures": [{{"structure": "…", "example": "…"}}], "tips": ["…", "…", "…"]}}
'''


# ============================================================ API routes
@app.post("/api/news")
def api_news():
    d = body()
    key = (d.get("news_key") or "").strip()
    if not key:
        return jsonify({"error": "Missing NewsAPI key"}), 400

    # Try several strategies because NewsAPI's free tier keeps tightening its rules.
    attempts = [
        # 1. top-headlines for France
        ("https://newsapi.org/v2/top-headlines",
         {"country": "fr", "pageSize": 12, "apiKey": key}),
        # 2. top-headlines with a French category
        ("https://newsapi.org/v2/top-headlines",
         {"country": "fr", "category": "general", "pageSize": 12, "apiKey": key}),
        # 3. everything, restricted to a French-language query
        ("https://newsapi.org/v2/everything",
         {"q": "France", "language": "fr", "sortBy": "publishedAt",
          "pageSize": 12, "apiKey": key}),
        # 4. everything, broader French query
        ("https://newsapi.org/v2/everything",
         {"q": "actualité", "language": "fr", "sortBy": "publishedAt",
          "pageSize": 12, "apiKey": key}),
        # 5. everything, with a domain filter as a last resort
        ("https://newsapi.org/v2/everything",
         {"q": "monde", "language": "fr", "sortBy": "publishedAt",
          "domains": "lemonde.fr,lefigaro.fr,franceinfo.fr,rf.fr",
          "pageSize": 12, "apiKey": key}),
    ]

    last_err = "unknown error"
    data = None
    for url, params in attempts:
        try:
            r = requests.get(url, params=params, timeout=30)
            j = r.json()
        except Exception as e:
            last_err = f"network: {e}"
            continue

        if j.get("status") == "ok" and j.get("articles"):
            data = j
            break

        last_err = j.get("message") or f"HTTP {r.status_code}"
        # If it's a key problem or a rate limit, retrying the other strategies only burns quota.
        if r.status_code in (401, 403):
            return jsonify({"error": last_err}), 400
        if r.status_code == 429:
            return jsonify({"error": last_err or "NewsAPI rate limit reached",
                            "hint": "You hit NewsAPI's request limit. Wait a while and retry."}), 429

    if data is None:
        return jsonify({
            "error": last_err,
            "hint": "NewsAPI free tier only allows news from the last 24h. "
                    "If you just signed up, wait a minute and retry.",
        }), 400

    arts = []
    for a in data.get("articles", []):
        if not a.get("title") or a["title"] == "[Removed]":
            continue
        arts.append({
            "title": a.get("title", ""),
            "description": a.get("description") or "",
            "content": a.get("content") or "",
            "url": a.get("url", ""),
            "source": (a.get("source") or {}).get("name", ""),
            "publishedAt": a.get("publishedAt", ""),
        })

    if not arts:
        return jsonify({"error": "No usable articles found. Try again later."}), 400

    return jsonify({"articles": arts})


@app.post("/api/adapt")
def api_adapt():
    d = body()
    try:
        out = ai_json(
            d.get("gemini_key", ""), d.get("openrouter_key", ""),
            p_adapt((d.get("text") or "")[:6000], norm_level(d.get("level")), d.get("title", "")),
        )
        adapted = str(out.get("text") or "").strip()
        if not adapted:
            return no_result("article")
        return jsonify({"title": str(out.get("title") or d.get("title") or ""), "text": adapted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/questions")
def api_questions():
    d = body()
    try:
        out = ai_json(
            d.get("gemini_key", ""), d.get("openrouter_key", ""),
            p_questions((d.get("text") or "")[:6000], norm_level(d.get("level")),
                        variant=d.get("variant", "first")),
            temperature=0.9,
        )
        qs = clean_questions(out.get("questions"))
        if not qs:
            return no_result("questions")
        return jsonify({"questions": qs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/writing_a")
def api_writing_a():
    d = body()
    try:
        out = ai_json(d.get("gemini_key", ""), d.get("openrouter_key", ""),
                      p_writing_a((d.get("text") or "")[:6000], norm_level(d.get("level"))))
        qs = [q for q in out.get("questions", []) if isinstance(q, dict) and q.get("question")] \
            if isinstance(out.get("questions"), list) else []
        if not qs:
            return no_result("questions")
        return jsonify({"questions": qs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/writing_b")
def api_writing_b():
    d = body()
    try:
        out = ai_json(d.get("gemini_key", ""), d.get("openrouter_key", ""),
                      p_writing_b((d.get("text") or "")[:6000], norm_level(d.get("level"))))
        items = [i for i in out.get("items", []) if isinstance(i, dict) and i.get("structure")] \
            if isinstance(out.get("items"), list) else []
        if not items:
            return no_result("items")
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/check_writing")
def api_check_writing():
    d = body()
    try:
        out = ai_json(d.get("gemini_key", ""), d.get("openrouter_key", ""),
                      p_check_writing(items_list(d), norm_level(d.get("level"))),
                      temperature=0.3)
        return jsonify({"results": clean_results(out.get("results"), with_errors=True)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/dictation")
def api_dictation():
    d = body()
    try:
        out = ai_json(d.get("gemini_key", ""), d.get("openrouter_key", ""),
                      p_dictation((d.get("text") or "")[:6000], norm_level(d.get("level"))))
        sents = [x.strip() for x in out.get("sentences", []) if isinstance(x, str) and x.strip()] \
            if isinstance(out.get("sentences"), list) else []
        if not sents:
            return no_result("sentences")
        return jsonify({"sentences": sents})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/check_dictation")
def api_check_dictation():
    d = body()
    try:
        out = ai_json(d.get("gemini_key", ""), d.get("openrouter_key", ""),
                      p_check_dictation(items_list(d), norm_level(d.get("level"))),
                      temperature=0.2)
        return jsonify({"results": clean_results(out.get("results"))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/cheatsheet")
def api_cheatsheet():
    d = body()
    try:
        out = ai_json(
            d.get("gemini_key", ""), d.get("openrouter_key", ""),
            p_cheatsheet((d.get("text") or "")[:6000], norm_level(d.get("level")),
                         d.get("structures", []), d.get("sentences", [])),
            temperature=0.5,
        )
        return jsonify({
            "vocab": out.get("vocab", []),
            "structures": out.get("structures", []),
            "tips": out.get("tips", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================ Archive / sessions
# Session ids are used in Cloudinary public_ids, so only allow a safe alphabet
# (the old code accepted e.g. "../../x" from the client).
SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def session_public_id(sid):
    # Cloudinary *raw* assets keep their file extension as part of the public_id, and
    # `format=` is ignored for raw uploads. Store the ".json" explicitly so the delivery
    # URL we build later actually exists.
    return f"{CLOUDINARY_FOLDER}/sessions/{sid}.json"


def legacy_session_public_ids(sid):
    return [f"{CLOUDINARY_FOLDER}/sessions/{sid}"]   # what earlier versions uploaded


@app.post("/api/sessions")
def api_save_session():
    """Create or update an archived lesson session. With Cloudinary backup."""
    d = body()
    sid = str(d.get("id") or "").strip() or str(uuid.uuid4())
    if not SID_RE.match(sid):
        return jsonify({"error": "Invalid session id"}), 400
    title = str(d.get("title") or "Untitled lesson").strip() or "Untitled lesson"
    source = str(d.get("source") or "").strip()
    level = norm_level(d.get("level"))
    last_step = str(d.get("last_step") or "news").strip()
    data_obj = d.get("data") if isinstance(d.get("data"), dict) else {}
    data = json.dumps(data_obj, ensure_ascii=False)
    ts = now_iso()

    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, title, source, level, last_step, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, title=excluded.title, "
            "source=excluded.source, level=excluded.level, last_step=excluded.last_step, data=excluded.data",
            (sid, ts, ts, title, source, level, last_step, data),
        )
        conn.commit()
        created_at = conn.execute("SELECT created_at FROM sessions WHERE id = ?", (sid,)).fetchone()["created_at"]

    if USE_CLOUDINARY and cloudinary_uploader:
        try:
            payload = json.dumps({"id": sid, "title": title, "source": source, "level": level,
                                  "last_step": last_step, "created_at": created_at, "updated_at": ts,
                                  "data": data_obj}, ensure_ascii=False).encode("utf-8")
            # BytesIO instead of a hand-built /tmp/<client-supplied-id>.json path
            cloudinary_uploader.upload(io.BytesIO(payload), resource_type="raw",
                                       public_id=session_public_id(sid), overwrite=True, invalidate=True)
        except Exception as e:
            print(f"Cloudinary session backup failed: {e}")

    return jsonify({"id": sid, "updated_at": ts, "cloudinary": USE_CLOUDINARY})


@app.get("/api/sessions")
def api_list_sessions():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT id, created_at, updated_at, title, source, level, last_step FROM sessions ORDER BY updated_at DESC LIMIT 200").fetchall()
    if not rows and USE_CLOUDINARY and cloudinary_api:
        try:
            res = cloudinary_api.resources(type="upload", resource_type="raw", prefix=f"{CLOUDINARY_FOLDER}/sessions/", max_results=100)
            sessions = []
            for r in res.get("resources", []):
                pid = r.get("public_id", "")
                sid = pid.split("/")[-1]
                if sid.endswith(".json"):
                    sid = sid[:-5]
                if not SID_RE.match(sid):
                    continue
                sessions.append({"id": sid, "created_at": r.get("created_at"), "updated_at": r.get("created_at"), "title": sid, "source": "cloudinary", "level": "?", "last_step": "lesson"})
            sessions.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            return jsonify({"sessions": sessions, "cloudinary": True, "note": "Listed from Cloudinary — local DB empty."})
        except Exception as e:
            print(f"Cloudinary list failed: {e}")
    return jsonify({"sessions": [dict(r) for r in rows], "cloudinary": USE_CLOUDINARY})


@app.get("/api/sessions/<sid>")
def api_get_session(sid):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if row:
        out = dict(row)
        try:
            out["data"] = json.loads(out["data"])
        except Exception:
            out["data"] = {}
        return jsonify(out)

    if USE_CLOUDINARY and cloudinary_utils and SID_RE.match(sid):
        for pid in [session_public_id(sid)] + legacy_session_public_ids(sid):
            try:
                url, _ = cloudinary_utils.cloudinary_url(pid, resource_type="raw")
                r = requests.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                j = r.json()
                if not isinstance(j, dict):
                    continue
                data_obj = j.get("data") if isinstance(j.get("data"), dict) else {}
                with closing(get_db()) as conn:
                    conn.execute("INSERT OR REPLACE INTO sessions (id, created_at, updated_at, title, source, level, last_step, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (sid, j.get("created_at") or now_iso(), j.get("updated_at") or now_iso(), j.get("title") or "",
                                  j.get("source") or "", norm_level(j.get("level")), j.get("last_step") or "lesson",
                                  json.dumps(data_obj, ensure_ascii=False)))
                    conn.commit()
                return jsonify({"id": sid, "title": j.get("title"), "source": j.get("source"), "level": j.get("level"),
                                "last_step": j.get("last_step"), "data": data_obj, "cloudinary": True})
            except Exception as e:
                print(f"Cloudinary restore failed ({pid}): {e}")

    return jsonify({"error": "Session not found"}), 404


@app.delete("/api/sessions/<sid>")
def api_delete_session(sid):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        conn.commit()
    if USE_CLOUDINARY and cloudinary_uploader and SID_RE.match(sid):
        for pid in [session_public_id(sid)] + legacy_session_public_ids(sid):
            try:
                cloudinary_uploader.destroy(pid, resource_type="raw", invalidate=True)
            except Exception as e:
                print(f"Cloudinary delete session failed ({pid}): {e}")
    return jsonify({"ok": True})


MAX_TTS_CHARS = 6000
RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


@app.post("/api/tts")
def api_tts():
    d = body()
    text = str(d.get("text") or "").strip()
    voice = str(d.get("voice") or "fr-FR-DeniseNeural")
    rate = str(d.get("rate") or "+0%")
    if not text:
        return jsonify({"error": "No text"}), 400
    if len(text) > MAX_TTS_CHARS:
        return jsonify({"error": f"Text too long (max {MAX_TTS_CHARS} characters)"}), 400
    if not VOICE_RE.match(voice):
        return jsonify({"error": "Invalid voice"}), 400
    if not RATE_RE.match(rate):
        return jsonify({"error": "Invalid rate (expected e.g. '-25%')"}), 400

    sha = hashlib.sha1(f"{voice}|{rate}|{text}".encode("utf-8")).hexdigest()
    name = sha + ".mp3"
    public_id = f"{CLOUDINARY_FOLDER}/audio/{sha}"
    path = os.path.join(AUDIO_DIR, name)

    # A failed/interrupted earlier run used to leave a partial (or empty) file at `path`, and
    # os.path.exists() then served that broken audio forever. Synthesize into a unique temp file
    # and atomically move it into place only when it is complete (also safe for concurrent requests).
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            import edge_tts

            async def gen():
                await edge_tts.Communicate(text, voice, rate=rate).save(tmp)
            asyncio.run(gen())
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                raise RuntimeError("no audio produced")
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return jsonify({"error": f"TTS failed: {e}"}), 500

    if USE_CLOUDINARY and cloudinary_uploader and cloudinary_api and cloudinary_utils:
        try:
            try:
                existing = cloudinary_api.resource(public_id, resource_type="video")
                url = existing.get("secure_url")
            except Exception:
                res = cloudinary_uploader.upload(path, resource_type="video", public_id=public_id, overwrite=False)
                url = res.get("secure_url")
            return jsonify({"url": url, "cloudinary": True, "public_id": public_id})
        except Exception as e:
            print(f"Cloudinary upload failed, fallback: {e}")
            return jsonify({"url": f"/audio_cache/{name}", "cloudinary": False, "error": str(e)})
    else:
        return jsonify({"url": f"/audio_cache/{name}", "cloudinary": False})


@app.get("/audio_cache/<path:filename>")
def audio_cache(filename):
    return send_from_directory(AUDIO_DIR, filename, mimetype="audio/mpeg")


@app.get("/api/cloudinary/status")
def cloudinary_status():
    return jsonify({"enabled": USE_CLOUDINARY, "folder": CLOUDINARY_FOLDER, "cloud_name": (os.getenv("CLOUDINARY_CLOUD_NAME")[:3]+"***" if os.getenv("CLOUDINARY_CLOUD_NAME") else None)})


@app.get("/static/srs.js")
def srs_js_route():
    return send_from_directory(STATIC_DIR, "srs.js", mimetype="application/javascript")


@app.get("/static/glossika_clean.json")
def glossika_json_route():
    return send_from_directory(STATIC_DIR, "glossika_clean.json", mimetype="application/json")


@app.get("/glossika_clean.json")
def glossika_root():
    return send_from_directory(STATIC_DIR, "glossika_clean.json", mimetype="application/json")



# ============================================================ Embedded frontend
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>French News Lab</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>

<header class="topbar">
  <div class="brand"><span class="flag">🇫🇷</span><h1>French News Lab</h1></div>
  <nav class="progress" id="progress">
    <span data-step="keys">1 · Keys</span>
    <span data-step="news">2 · News</span>
    <span data-step="lesson">3 · Lesson</span>
    <span data-step="part1">4 · Listening</span>
    <span data-step="part2">5 · Transcript</span>
    <span data-step="writingA">6 · Writing A</span>
    <span data-step="writingB">7 · Writing B</span>
    <span data-step="dictation">8 · Dictation</span>
    <span data-step="cheatsheet">9 · Cheat Sheet</span>
  </nav>
  <button class="ghost small" id="btnArchive">📂 Archive</button>
</header>

<!-- ARCHIVE OVERLAY -->
<div class="overlay hidden" id="archiveOverlay">
  <div class="overlay-card">
    <div class="overlay-head">
      <h2>📂 Archived lessons</h2>
      <button class="ghost small" id="btnCloseArchive">✕ Close</button>
    </div>
    <p class="muted">Past news, generated questions, answers and audio are cached — resuming a lesson replays it without new API calls (only re-checking answers calls the API again).</p>
    <div id="archiveList" class="archive-list"></div>
  </div>
</div>

<main>
  <!-- 1 · KEYS -->
  <section class="view" id="view-keys">
    <div class="card">
      <h2>1 · API keys</h2>
      <p class="muted">Keys live only in your browser's localStorage and are sent to this local Flask server to call the APIs.</p>

      <label class="field">
        <span>Gemini API key</span>
        <input type="password" id="inpGemini" placeholder="AIza..." autocomplete="off">
        <small><a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">Get a Gemini key →</a></small>
      </label>

      <label class="field">
        <span>NewsAPI key</span>
        <input type="password" id="inpNews" placeholder="your newsapi.org key" autocomplete="off">
        <small><a href="https://newsapi.org/register" target="_blank" rel="noopener">Get a NewsAPI key →</a></small>
      </label>

      <label class="field">
        <span>OpenRouter API key — optional fallback (Qwen, free tier)</span>
        <input type="password" id="inpOpenRouter" placeholder="sk-or-..." autocomplete="off">
        <small>Used only if Gemini fails or is out of quota. <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">Get an OpenRouter key →</a></small>
      </label>

      <label class="field">
        <span>French voice</span>
        <select id="inpVoice">
          <option value="fr-FR-DeniseNeural">Denise (France, female)</option>
          <option value="fr-FR-HenriNeural">Henri (France, male)</option>
          <option value="fr-FR-EloiseNeural">Éloïse (France, young)</option>
          <option value="fr-CA-SylvieNeural">Sylvie (Canada, female)</option>
          <option value="fr-CA-JeanNeural">Jean (Canada, male)</option>
        </select>
      </label>

      <button class="primary" id="btnSaveKeys">Save &amp; continue →</button>
    </div>
  </section>

  <!-- 2 · NEWS -->
  <section class="view hidden" id="view-news">
    <div class="card">
      <h2>2 · Today's French news</h2>
      <div class="row">
        <button class="primary" id="btnLoadNews">Load today's headlines</button>
        <button class="ghost" id="btnBackKeys">← Keys</button>
      </div>
      <p class="muted" id="newsStatus"></p>
      <div class="news-list" id="newsList"></div>
    </div>
  </section>

  <!-- 3 · LESSON -->
  <section class="view hidden" id="view-lesson">
    <div class="card">
      <h2>3 · Adapt the article</h2>
      <div class="article-preview" id="chosenArticle"></div>
      <div class="level-picker">
        <span class="muted">Choose your level:</span>
        <div class="levels" id="levelButtons">
          <button data-level="A1" class="level-btn active">A1</button>
          <button data-level="A2" class="level-btn">A2</button>
          <button data-level="B1" class="level-btn">B1</button>
          <button data-level="B2" class="level-btn">B2</button>
        </div>
      </div>
      <div class="row">
        <button class="primary" id="btnAdapt">✨ Adapt with Gemini</button>
        <button class="ghost" id="btnBackNews">← News</button>
      </div>
      <p class="muted" id="adaptStatus"></p>
    </div>

    <div class="card hidden" id="adaptedCard">
      <h2 id="adaptedTitle"></h2>
      <div class="audio-row">
        <button class="play-btn big" id="btnPlayAdapted">🔊 Play the article</button>
        <span class="muted" id="adaptedMeta"></span>
      </div>
      <div class="french-text" id="adaptedText"></div>
      <div class="row">
        <button class="primary" id="btnToPart1">Continue to listening test →</button>
      </div>
    </div>
  </section>

  <!-- 4 · PART 1 -->
  <section class="view hidden" id="view-part1">
    <div class="card">
      <h2>4 · Part 1 — Listening comprehension</h2>
      <p class="muted">Listen to the article again. No transcript. Then answer the questions. Each question is read aloud — click 🔊 to hear it again.</p>
      <div class="audio-row">
        <button class="play-btn big" id="btnPlayP1">🔊 Play the article</button>
      </div>
      <div id="part1Loading" class="loading hidden">Generating questions with Gemini…</div>
      <div class="quiz" id="part1Quiz"></div>
      <div class="row">
        <button class="primary" id="btnCheckP1">Check answers</button>
        <span class="score" id="scoreP1"></span>
        <button class="primary hidden" id="btnToPart2">Continue →</button>
      </div>
    </div>
  </section>

  <!-- 5 · PART 2 -->
  <section class="view hidden" id="view-part2">
    <div class="card">
      <h2>5 · Part 2 — Listen with transcript</h2>
      <p class="muted">Follow along with the transcript, then answer the second set of questions.</p>
      <div class="audio-row">
        <button class="play-btn big" id="btnPlayP2">🔊 Play the article</button>
      </div>
      <div class="french-text" id="part2Transcript"></div>
      <div id="part2Loading" class="loading hidden">Generating questions with Gemini…</div>
      <div class="quiz" id="part2Quiz"></div>
      <div class="row">
        <button class="primary" id="btnCheckP2">Check answers</button>
        <span class="score" id="scoreP2"></span>
        <button class="primary hidden" id="btnToWritingA">Continue →</button>
      </div>
    </div>
  </section>

  <!-- 6 · WRITING A -->
  <section class="view hidden" id="view-writingA">
    <div class="card">
      <h2>6 · Writing — Section A</h2>
      <p class="muted">Answer each question in a full French sentence. Gemini will correct your grammar, vocabulary and content.</p>
      <div id="writingALoading" class="loading hidden">Generating questions with Gemini…</div>
      <div id="writingAForm" class="writing-form"></div>
      <div class="row">
        <button class="primary" id="btnCheckWA">Check my writing</button>
        <span class="score" id="scoreWA"></span>
        <button class="primary hidden" id="btnToWritingB">Continue →</button>
      </div>
    </div>
  </section>

  <!-- 7 · WRITING B -->
  <section class="view hidden" id="view-writingB">
    <div class="card">
      <h2>7 · Writing — Section B</h2>
      <p class="muted">Write your own French sentence using each structure / vocabulary item from the article.</p>
      <div id="writingBLoading" class="loading hidden">Extracting structures with Gemini…</div>
      <div id="writingBForm" class="writing-form"></div>
      <div class="row">
        <button class="primary" id="btnCheckWB">Check my sentences</button>
        <span class="score" id="scoreWB"></span>
        <button class="primary hidden" id="btnToDictation">Continue →</button>
      </div>
    </div>
  </section>

  <!-- 8 · DICTATION -->
  <section class="view hidden" id="view-dictation">
    <div class="card">
      <h2>8 · Dictation</h2>
      <p class="muted">Listen to each sentence (as many times as you like) and write exactly what you hear. Accents count!</p>
      <div id="dictLoading" class="loading hidden">Preparing dictation with Gemini…</div>
      <div id="dictForm" class="writing-form"></div>
      <div class="row">
        <button class="primary" id="btnCheckDict">Check my dictation</button>
        <span class="score" id="scoreDict"></span>
        <button class="primary hidden" id="btnToCheatSheet">Continue to cheat sheet →</button>
      </div>
    </div>
  </section>

  <!-- 9 · CHEAT SHEET -->
  <section class="view hidden" id="view-cheatsheet">
    <div class="card">
      <h2>9 · Cheat sheet — quick review</h2>
      <p class="muted">A summary of the vocabulary and structures from this lesson, for a last quick review.</p>
      <div id="cheatLoading" class="loading hidden">Building your cheat sheet…</div>
      <div id="cheatContent" class="cheat-content"></div>
      <div class="row">
        <button class="ghost" id="btnRestart">🔄 Start over with another article</button>
      </div>
    </div>
  </section>
</main>

<div class="player-bar">
  <span class="player-label" id="playerLabel">🔊 Ready</span>
  <audio id="player" controls preload="none"></audio>
</div>

<script src="/static/app.js"></script>
</body>
</html>
"""


STYLE_CSS = r"""
:root {
  --bg: #0f172a; --panel: #1e293b; --panel-2: #334155;
  --text: #e2e8f0; --muted: #94a3b8;
  --accent: #60a5fa; --good: #34d399; --bad: #f87171; --warn: #fbbf24;
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: linear-gradient(180deg, #0f172a, #1e293b 40%, #0f172a);
  color: var(--text); min-height: 100vh; padding-bottom: 120px;
}
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 24px; border-bottom: 1px solid var(--panel-2);
  background: rgba(15,23,42,.85); backdrop-filter: blur(6px);
  position: sticky; top: 0; z-index: 10; flex-wrap: wrap; gap: 10px;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand h1 { font-size: 18px; margin: 0; letter-spacing: .3px; }
.flag { font-size: 22px; }
.progress { display: flex; gap: 4px; font-size: 12px; flex-wrap: wrap; }
.progress span {
  padding: 4px 8px; border-radius: 999px; color: var(--muted);
  border: 1px solid transparent; transition: all .2s;
}
.progress span.done { color: var(--good); cursor: pointer; }
.progress span.active { color: var(--accent); border-color: var(--accent); cursor: pointer; }
.progress span.unlocked { cursor: pointer; }
.progress span.unlocked:hover { border-color: var(--panel-2); background: rgba(255,255,255,.05); }
.progress span.locked { opacity: .45; cursor: not-allowed; }
button.small { padding: 5px 10px; font-size: 12px; }
main { max-width: 860px; margin: 24px auto; padding: 0 16px; }
.view.hidden, .card.hidden { display: none; }
.card {
  background: var(--panel); border: 1px solid var(--panel-2);
  border-radius: 14px; padding: 22px; margin-bottom: 18px;
  box-shadow: 0 6px 24px rgba(0,0,0,.25);
}
h2 { margin: 0 0 12px; font-size: 20px; }
h3 { margin: 6px 0; font-size: 17px; }
.muted { color: var(--muted); font-size: 13px; }
.field { display: block; margin: 14px 0; }
.field span { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.field input, .field select {
  width: 100%; padding: 10px 12px; border-radius: 8px;
  border: 1px solid var(--panel-2); background: #0b1220; color: var(--text);
  font-size: 14px; font-family: inherit;
}
.field small { display: block; margin-top: 6px; font-size: 12px; }
.field a { color: var(--accent); text-decoration: none; }
button {
  font: inherit; cursor: pointer; border-radius: 8px;
  border: 1px solid var(--panel-2); background: var(--panel-2); color: var(--text);
  padding: 9px 14px; transition: all .15s;
}
button:hover:not(:disabled) { filter: brightness(1.15); }
button:disabled { opacity: .5; cursor: not-allowed; }
button.primary { background: var(--accent); color: #0b1220; border-color: transparent; font-weight: 600; }
button.ghost { background: transparent; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
.news-list { display: grid; gap: 10px; margin-top: 12px; }
.news-item {
  padding: 12px 14px; border-radius: 10px; cursor: pointer;
  background: #0b1220; border: 1px solid var(--panel-2);
  transition: border-color .15s, transform .1s;
}
.news-item:hover { border-color: var(--accent); transform: translateY(-1px); }
.news-source { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
.news-title { font-weight: 600; margin: 4px 0; }
.news-desc { font-size: 13px; color: var(--muted); }
.article-preview {
  background: #0b1220; padding: 14px; border-radius: 10px;
  border: 1px solid var(--panel-2); margin-bottom: 14px;
}
.level-picker { display: flex; align-items: center; gap: 12px; margin: 14px 0; flex-wrap: wrap; }
.levels { display: flex; gap: 6px; }
.level-btn { padding: 6px 16px; border-radius: 999px; background: var(--panel-2); }
.level-btn.active { background: var(--accent); color: #0b1220; font-weight: 700; }
.french-text {
  background: #0b1220; padding: 16px; border-radius: 10px; border: 1px solid var(--panel-2);
  white-space: pre-wrap; line-height: 1.65; font-size: 15px;
  font-family: Georgia, serif; max-height: 400px; overflow-y: auto;
}
.audio-row { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; flex-wrap: wrap; }
.play-btn {
  background: var(--accent); color: #0b1220; border: none; font-weight: 600;
  padding: 10px 18px; border-radius: 999px;
}
.play-btn.big { font-size: 15px; padding: 12px 22px; }
.play-btn.small { padding: 5px 12px; font-size: 13px; }
.quiz { display: grid; gap: 18px; margin-top: 10px; }
.quiz-q { background: #0b1220; padding: 16px; border-radius: 10px; border: 1px solid var(--panel-2); }
.q-head { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
.q-num { color: var(--accent); font-weight: 700; }
.q-text { flex: 1; font-weight: 500; }
.speak-btn {
  background: var(--panel-2); border: none; padding: 4px 10px;
  border-radius: 6px; font-size: 14px;
}
.quiz-opts { display: grid; gap: 8px; }
.opt {
  text-align: left; padding: 10px 14px; border-radius: 8px;
  background: #131c2e; border: 1px solid var(--panel-2);
}
.opt:hover:not(:disabled) { border-color: var(--accent); }
.opt.sel { border-color: var(--accent); background: rgba(96,165,250,.12); }
.opt.good { background: rgba(52,211,153,.15); border-color: var(--good); }
.opt.bad { background: rgba(248,113,113,.15); border-color: var(--bad); }
.score { font-weight: 700; color: var(--good); margin-left: 8px; }
.writing-form { display: grid; gap: 18px; }
.writing-q { background: #0b1220; padding: 16px; border-radius: 10px; border: 1px solid var(--panel-2); }
.writing-q label { display: block; }
.writing-q textarea {
  width: 100%; margin-top: 10px; padding: 10px 12px; border-radius: 8px;
  border: 1px solid var(--panel-2); background: #131c2e; color: var(--text);
  font: inherit; resize: vertical; font-size: 14px;
}
.hint { display: block; color: var(--warn); font-size: 12px; margin-top: 6px; }
.feedback {
  margin-top: 12px; padding: 12px; border-radius: 8px;
  background: #131c2e; border-left: 3px solid var(--accent); font-size: 13px;
}
.feedback.hidden { display: none; }
.fb-score { font-weight: 700; color: var(--good); margin-bottom: 6px; }
.fb-corrected, .fb-errors, .fb-comment { margin: 6px 0; }
.fb-errors ul { margin: 4px 0; padding-left: 18px; }
.loading {
  padding: 14px; text-align: center; color: var(--accent);
  background: rgba(96,165,250,.08); border-radius: 8px; margin: 12px 0;
  animation: pulse 1.4s ease-in-out infinite;
}
.loading.hidden { display: none; }
@keyframes pulse { 0%,100% { opacity: .6; } 50% { opacity: 1; } }
.player-bar {
  position: fixed; bottom: 0; left: 0; right: 0;
  background: rgba(15,23,42,.95); backdrop-filter: blur(8px);
  border-top: 1px solid var(--panel-2);
  padding: 10px 24px; display: flex; align-items: center; gap: 16px; z-index: 20;
}
.player-label { font-size: 13px; color: var(--muted); white-space: nowrap; }
.player-bar audio { flex: 1; height: 36px; }
.hidden { display: none !important; }

/* ---------------------------------------------------------- archive overlay */
.overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.55);
  display: flex; align-items: flex-start; justify-content: center;
  padding: 40px 16px; z-index: 50; overflow-y: auto;
}
.overlay.hidden { display: none; }
.overlay-card {
  background: var(--panel); border: 1px solid var(--panel-2); border-radius: 14px;
  padding: 22px; max-width: 720px; width: 100%; box-shadow: 0 12px 40px rgba(0,0,0,.4);
}
.overlay-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.overlay-head h2 { margin: 0; }
.archive-list { display: grid; gap: 10px; margin-top: 14px; }
.archive-item {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  background: #0b1220; border: 1px solid var(--panel-2); border-radius: 10px;
  padding: 12px 14px; flex-wrap: wrap;
}
.archive-item .meta { display: flex; flex-direction: column; gap: 2px; }
.archive-item .archive-title { font-weight: 600; }
.archive-item .archive-sub { font-size: 12px; color: var(--muted); }
.archive-item .archive-actions { display: flex; gap: 8px; }
.archive-empty { color: var(--muted); font-size: 13px; padding: 12px 0; }

/* ---------------------------------------------------------- cheat sheet */
.cheat-content { display: grid; gap: 18px; margin-top: 8px; }
.cheat-section h3 { margin-bottom: 10px; }
.cheat-vocab { display: grid; gap: 8px; }
.cheat-vocab-item, .cheat-struct-item {
  background: #0b1220; border: 1px solid var(--panel-2); border-radius: 8px;
  padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; gap: 10px;
}
.cheat-word { font-weight: 600; }
.cheat-meaning { color: var(--muted); font-size: 13px; }
.cheat-struct-item { flex-direction: column; align-items: flex-start; }
.cheat-struct-item .cheat-structure { font-weight: 600; }
.cheat-struct-item .cheat-example { color: var(--muted); font-size: 13px; font-style: italic; margin-top: 4px; }
.cheat-tips { padding-left: 18px; margin: 0; }
.cheat-tips li { margin: 4px 0; }
"""


APP_JS = r"""
const RATE_BY_LEVEL = { A1: '-25%', A2: '-15%', B1: '-5%', B2: '+0%' };

const state = {
  geminiKey: '', newsKey: '', openrouterKey: '', voice: 'fr-FR-DeniseNeural',
  sessionId: null, lastStep: 'keys', maxStepIndex: 0,
  articles: [], chosen: null, level: 'A1', adapted: null,
  part1Questions: [], part2Questions: [],
  writingA: [], writingB: [], dictation: [], cheatsheet: null,
  writingAAnswers: [], writingBAnswers: [], dictationAnswers: [],
  writingAResults: [], writingBResults: [], dictationResults: [],
};

const $ = id => document.getElementById(id);
const ORDER = ['keys','news','lesson','part1','part2','writingA','writingB','dictation','cheatsheet'];
// Maps a step name to the loader that renders it (from cache when possible, else fetches).
const STEP_GO = {
  keys: () => showView('keys'),
  news: () => showView('news'),
  lesson: () => showView('lesson'),
  part1: goPart1,
  part2: goPart2,
  writingA: goWritingA,
  writingB: goWritingB,
  dictation: goDictation,
  cheatsheet: goCheatSheet,
};

function showView(name) {
  ORDER.forEach(v => $('view-' + v).classList.toggle('hidden', v !== name));
  state.lastStep = name;
  state.maxStepIndex = Math.max(state.maxStepIndex, ORDER.indexOf(name));
  setProgress(name);
  window.scrollTo(0, 0);
}

function setProgress(step) {
  const idx = ORDER.indexOf(step);
  document.querySelectorAll('#progress span').forEach((el, i) => {
    el.classList.toggle('active', i === idx);
    el.classList.toggle('done', i < idx);
    el.classList.toggle('locked', i > state.maxStepIndex);
    el.classList.toggle('unlocked', i <= state.maxStepIndex && i !== idx);
  });
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ---------------------------------------------------------- audio
let currentUrl = null;
async function speak(text, rate) {
  const player = $('player');
  $('playerLabel').textContent = '🔊 Loading…';
  try {
    const r = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, voice: state.voice, rate: rate || '+0%' })
    });
    if (!r.ok) {
      const t = await r.text();
      $('playerLabel').textContent = '🔇 TTS error';
      console.error('TTS error:', t);
      return;
    }
    const blob = await r.blob();
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    currentUrl = URL.createObjectURL(blob);
    player.src = currentUrl;
    $('playerLabel').textContent = '🔊 Playing';
    player.onended = () => { $('playerLabel').textContent = '🔊 Done'; };
    await player.play();
  } catch (e) {
    $('playerLabel').textContent = '🔇 ' + e.message;
  }
}

async function playArticle() {
  if (!state.adapted) return;
  await speak(state.adapted.text, RATE_BY_LEVEL[state.level]);
}

// ---------------------------------------------------------- keys
function saveKeys() {
  const g = $('inpGemini').value.trim();
  const n = $('inpNews').value.trim();
  const x = $('inpOpenRouter').value.trim();
  if (!n) { alert('Please enter your NewsAPI key.'); return; }
  if (!g && !x) { alert('Please enter a Gemini key and/or an OpenRouter key.'); return; }
  state.geminiKey = g;
  state.newsKey = n;
  state.openrouterKey = x;
  state.voice = $('inpVoice').value;
  localStorage.setItem('geminiKey', g);
  localStorage.setItem('newsKey', n);
  localStorage.setItem('openrouterKey', x);
  localStorage.setItem('voice', state.voice);
  showView('news');
}

// ---------------------------------------------------------- news
async function loadNews() {
  $('newsStatus').textContent = 'Loading…';
  $('newsList').innerHTML = '';
  try {
    const r = await fetch('/api/news', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ news_key: state.newsKey })
    });
    const data = await r.json();
    if (data.error) {
      $('newsStatus').textContent = 'Error: ' + data.error +
        (data.hint ? ' — ' + data.hint : '');
      return;
    }
    state.articles = data.articles || [];
    $('newsStatus').textContent = state.articles.length + ' articles found. Click one to choose.';
    renderNewsList();
  } catch (e) {
    $('newsStatus').textContent = 'Error: ' + e.message;
  }
}

function renderNewsList() {
  const list = $('newsList');
  list.innerHTML = '';
  state.articles.forEach((a, i) => {
    const el = document.createElement('div');
    el.className = 'news-item';
    el.innerHTML = '<div class="news-source">' + escapeHtml(a.source) + ' · ' +
      escapeHtml(new Date(a.publishedAt).toLocaleString()) + '</div>' +
      '<div class="news-title">' + escapeHtml(a.title) + '</div>' +
      '<div class="news-desc">' + escapeHtml(a.description || '') + '</div>';
    el.onclick = () => chooseArticle(i);
    list.appendChild(el);
  });
}

function chooseArticle(i) {
  state.chosen = state.articles[i];
  const a = state.chosen;
  $('chosenArticle').innerHTML =
    '<div class="news-source">' + escapeHtml(a.source) + '</div>' +
    '<h3>' + escapeHtml(a.title) + '</h3>' +
    '<p>' + escapeHtml(a.description || '') + '</p>' +
    '<p class="muted">' + escapeHtml(a.content || '') + '</p>';
  $('adaptedCard').classList.add('hidden');
  $('adaptStatus').textContent = '';
  // Starting fresh on a new article opens a new archive entry (old news gets archived too).
  state.sessionId = null;
  state.part1Questions = []; state.part2Questions = [];
  state.writingA = []; state.writingB = []; state.dictation = []; state.cheatsheet = null;
  state.writingAAnswers = []; state.writingBAnswers = []; state.dictationAnswers = [];
  state.writingAResults = []; state.writingBResults = []; state.dictationResults = [];
  showView('lesson');
  saveSession();
}

// ---------------------------------------------------------- archive (sqlite-backed)
function buildSnapshot() {
  return {
    chosen: state.chosen, level: state.level, adapted: state.adapted,
    part1Questions: state.part1Questions, part2Questions: state.part2Questions,
    writingA: state.writingA, writingB: state.writingB, dictation: state.dictation,
    cheatsheet: state.cheatsheet,
    writingAAnswers: state.writingAAnswers, writingBAnswers: state.writingBAnswers,
    dictationAnswers: state.dictationAnswers,
    writingAResults: state.writingAResults, writingBResults: state.writingBResults,
    dictationResults: state.dictationResults,
  };
}

function applySnapshot(snap) {
  state.chosen = snap.chosen || null;
  state.level = snap.level || 'A1';
  state.adapted = snap.adapted || null;
  state.part1Questions = snap.part1Questions || [];
  state.part2Questions = snap.part2Questions || [];
  state.writingA = snap.writingA || [];
  state.writingB = snap.writingB || [];
  state.dictation = snap.dictation || [];
  state.cheatsheet = snap.cheatsheet || null;
  state.writingAAnswers = snap.writingAAnswers || [];
  state.writingBAnswers = snap.writingBAnswers || [];
  state.dictationAnswers = snap.dictationAnswers || [];
  state.writingAResults = snap.writingAResults || [];
  state.writingBResults = snap.writingBResults || [];
  state.dictationResults = snap.dictationResults || [];
  $('levelButtons').querySelectorAll('.level-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.level === state.level);
  });
  if (state.chosen) {
    const a = state.chosen;
    $('chosenArticle').innerHTML =
      '<div class="news-source">' + escapeHtml(a.source) + '</div>' +
      '<h3>' + escapeHtml(a.title) + '</h3>' +
      '<p>' + escapeHtml(a.description || '') + '</p>' +
      '<p class="muted">' + escapeHtml(a.content || '') + '</p>';
  }
  if (state.adapted) {
    $('adaptedTitle').textContent = state.adapted.title || '';
    $('adaptedText').textContent = state.adapted.text || '';
    const wc = (state.adapted.text || '').split(/\s+/).filter(Boolean).length;
    $('adaptedMeta').textContent = 'Level ' + state.level + ' · ' + wc + ' words';
    $('adaptedCard').classList.remove('hidden');
  }
}

async function saveSession() {
  if (!state.chosen) return; // nothing worth archiving yet
  try {
    const r = await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: state.sessionId,
        title: state.adapted?.title || state.chosen?.title || 'Untitled lesson',
        source: state.chosen?.source || '',
        level: state.level,
        last_step: state.lastStep,
        data: buildSnapshot(),
      })
    });
    const data = await r.json();
    if (data.id) state.sessionId = data.id;
  } catch (e) { console.error('saveSession failed:', e); }
}

async function openArchive() {
  $('archiveOverlay').classList.remove('hidden');
  $('archiveList').innerHTML = '<p class="archive-empty">Loading…</p>';
  try {
    const r = await fetch('/api/sessions');
    const data = await r.json();
    renderArchiveList(data.sessions || []);
  } catch (e) {
    $('archiveList').innerHTML = '<p class="archive-empty">Error: ' + escapeHtml(e.message) + '</p>';
  }
}

function closeArchive() { $('archiveOverlay').classList.add('hidden'); }

const STEP_LABELS = {
  keys: 'Keys', news: 'News', lesson: 'Lesson', part1: 'Listening', part2: 'Transcript',
  writingA: 'Writing A', writingB: 'Writing B', dictation: 'Dictation', cheatsheet: 'Cheat sheet',
};

function renderArchiveList(sessions) {
  const list = $('archiveList');
  if (!sessions.length) {
    list.innerHTML = '<p class="archive-empty">No archived lessons yet — finish a step and it will show up here.</p>';
    return;
  }
  list.innerHTML = '';
  sessions.forEach(s => {
    const el = document.createElement('div');
    el.className = 'archive-item';
    const when = new Date(s.updated_at).toLocaleString();
    el.innerHTML =
      '<div class="meta">' +
      '<span class="archive-title">' + escapeHtml(s.title) + '</span>' +
      '<span class="archive-sub">' + escapeHtml(s.source || '') + ' · Level ' + escapeHtml(s.level) +
      ' · ' + escapeHtml(STEP_LABELS[s.last_step] || s.last_step) + ' · ' + when + '</span>' +
      '</div>' +
      '<div class="archive-actions">' +
      '<button type="button" class="primary small resume-btn">Resume</button>' +
      '<button type="button" class="ghost small delete-btn">Delete</button>' +
      '</div>';
    el.querySelector('.resume-btn').onclick = () => resumeSession(s.id);
    el.querySelector('.delete-btn').onclick = () => deleteSession(s.id, el);
    list.appendChild(el);
  });
}

async function resumeSession(id) {
  try {
    const r = await fetch('/api/sessions/' + id);
    const s = await r.json();
    if (s.error) { alert(s.error); return; }
    state.sessionId = s.id;
    applySnapshot(s.data || {});
    closeArchive();
    const step = s.last_step && STEP_GO[s.last_step] ? s.last_step : 'lesson';
    state.maxStepIndex = Math.max(state.maxStepIndex, ORDER.indexOf(step));
    STEP_GO[step]();
  } catch (e) { alert('Error resuming: ' + e.message); }
}

async function deleteSession(id, el) {
  if (!confirm('Delete this archived lesson? This cannot be undone.')) return;
  try {
    await fetch('/api/sessions/' + id, { method: 'DELETE' });
    el.remove();
    if (id === state.sessionId) state.sessionId = null;
  } catch (e) { alert('Error deleting: ' + e.message); }
}

// ---------------------------------------------------------- adapt
async function adapt() {
  if (!state.chosen) { alert('Pick an article first.'); return; }
  $('adaptStatus').textContent = '✨ Adapting with Gemini…';
  try {
    const text = (state.chosen.content || '') + '\n\n' + (state.chosen.description || '');
    const r = await fetch('/api/adapt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey,
        level: state.level,
        title: state.chosen.title,
        text
      })
    });
    const data = await r.json();
    if (data.error) { $('adaptStatus').textContent = 'Error: ' + data.error; return; }
    state.adapted = data;
    $('adaptedTitle').textContent = data.title;
    $('adaptedText').textContent = data.text;
    const wc = data.text.split(/\s+/).filter(Boolean).length;
    $('adaptedMeta').textContent = 'Level ' + state.level + ' · ' + wc + ' words';
    $('adaptedCard').classList.remove('hidden');
    $('adaptStatus').textContent = '✅ Done. Listen, then continue.';
    saveSession();
  } catch (e) {
    $('adaptStatus').textContent = 'Error: ' + e.message;
  }
}

// ---------------------------------------------------------- quiz render / check
function renderQuiz(part) {
  const qs = part === 'part1' ? state.part1Questions : state.part2Questions;
  const container = $(part === 'part1' ? 'part1Quiz' : 'part2Quiz');
  container.innerHTML = '';
  qs.forEach((q, qi) => {
    const block = document.createElement('div');
    block.className = 'quiz-q';
    const head = document.createElement('div');
    head.className = 'q-head';
    head.innerHTML = '<span class="q-num">' + (qi + 1) + '.</span>' +
      '<span class="q-text">' + escapeHtml(q.question) + '</span>' +
      '<button type="button" class="speak-btn" title="Play question">🔊</button>';
    head.querySelector('.speak-btn').onclick = () =>
      speak(q.question, RATE_BY_LEVEL[state.level]);
    block.appendChild(head);
    const opts = document.createElement('div');
    opts.className = 'quiz-opts';
    q.options.forEach((o, oi) => {
      const btn = document.createElement('button');
      btn.className = 'opt';
      btn.textContent = o;
      btn.onclick = () => {
        opts.querySelectorAll('.opt').forEach(x => x.classList.remove('sel'));
        btn.classList.add('sel');
        q._selected = oi;
      };
      opts.appendChild(btn);
    });
    block.appendChild(opts);
    container.appendChild(block);
    // Restore a previous selection/grading when navigating back to this step.
    if (q._checked) {
      opts.querySelectorAll('.opt').forEach((btn, oi) => {
        if (oi === q.answer_index) btn.classList.add('good');
        else if (q._selected === oi) btn.classList.add('bad');
        btn.disabled = true;
      });
    } else if (q._selected != null) {
      opts.querySelectorAll('.opt')[q._selected]?.classList.add('sel');
    }
  });
  if (qs.length && qs.every(q => q._checked)) {
    const correct = qs.filter(q => q._selected === q.answer_index).length;
    const pct = Math.round(100 * correct / qs.length);
    $(part === 'part1' ? 'scoreP1' : 'scoreP2').textContent =
      correct + '/' + qs.length + ' (' + pct + '%)';
    if (part === 'part1') $('btnToPart2').classList.remove('hidden');
    else $('btnToWritingA').classList.remove('hidden');
  }
}

function checkQuiz(part) {
  const qs = part === 'part1' ? state.part1Questions : state.part2Questions;
  const container = $(part === 'part1' ? 'part1Quiz' : 'part2Quiz');
  let correct = 0;
  container.querySelectorAll('.quiz-q').forEach((block, qi) => {
    const q = qs[qi];
    block.querySelectorAll('.opt').forEach((btn, oi) => {
      btn.classList.remove('good', 'bad');
      if (oi === q.answer_index) btn.classList.add('good');
      else if (q._selected === oi) btn.classList.add('bad');
      btn.disabled = true;
    });
    q._checked = true;
    if (q._selected === q.answer_index) correct++;
  });
  const pct = Math.round(100 * correct / qs.length);
  $(part === 'part1' ? 'scoreP1' : 'scoreP2').textContent =
    correct + '/' + qs.length + ' (' + pct + '%)';
  saveSession();
  if (part === 'part1') $('btnToPart2').classList.remove('hidden');
  else $('btnToWritingA').classList.remove('hidden');
}

async function goPart1() {
  showView('part1');
  if (state.part1Questions.length) { renderQuiz('part1'); return; }
  $('part1Loading').classList.remove('hidden');
  try {
    const r = await fetch('/api/questions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level,
        text: state.adapted.text, variant: 'first'
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.part1Questions = data.questions || [];
    renderQuiz('part1');
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('part1Loading').classList.add('hidden'); }
}

async function goPart2() {
  showView('part2');
  $('part2Transcript').textContent = state.adapted.text;
  if (state.part2Questions.length) { renderQuiz('part2'); return; }
  $('part2Loading').classList.remove('hidden');
  try {
    const r = await fetch('/api/questions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level,
        text: state.adapted.text, variant: 'second'
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.part2Questions = data.questions || [];
    renderQuiz('part2');
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('part2Loading').classList.add('hidden'); }
}

// ---------------------------------------------------------- writing A
async function goWritingA() {
  showView('writingA');
  if (state.writingA.length) { renderWritingA(); return; }
  $('writingALoading').classList.remove('hidden');
  try {
    const r = await fetch('/api/writing_a', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level, text: state.adapted.text
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.writingA = data.questions || [];
    renderWritingA();
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('writingALoading').classList.add('hidden'); }
}

function renderWritingA() {
  const form = $('writingAForm');
  form.innerHTML = '';
  state.writingA.forEach((q, i) => {
    const block = document.createElement('div');
    block.className = 'writing-q';
    block.innerHTML =
      '<div class="q-head"><span class="q-num">' + (i + 1) + '.</span>' +
      '<span class="q-text">' + escapeHtml(q.question) + '</span>' +
      '<button type="button" class="speak-btn">🔊</button></div>' +
      (q.hint ? '<small class="hint">💡 ' + escapeHtml(q.hint) + '</small>' : '') +
      '<textarea rows="3" placeholder="Écris ta réponse en français…"></textarea>' +
      '<div class="feedback hidden"></div>';
    block.querySelector('.speak-btn').onclick = () =>
      speak(q.question, RATE_BY_LEVEL[state.level]);
    const ta = block.querySelector('textarea');
    ta.value = state.writingAAnswers[i] || '';
    ta.oninput = () => { state.writingAAnswers[i] = ta.value; };
    form.appendChild(block);
  });
  if (state.writingAResults.length === state.writingA.length && state.writingA.length) {
    renderWritingFeedback('A', state.writingAResults);
  }
}

async function checkWriting(part) {
  const items = [];
  const prompts = part === 'A' ? state.writingA : state.writingB;
  const container = part === 'A' ? $('writingAForm') : $('writingBForm');
  container.querySelectorAll('textarea').forEach((ta, i) => {
    const p = prompts[i] || {};
    items.push({
      index: i,
      prompt: part === 'A' ? p.question : ((p.structure || '') + ' — ' + (p.meaning || '')),
      answer: ta.value.trim()
    });
  });
  const btn = part === 'A' ? $('btnCheckWA') : $('btnCheckWB');
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = 'Checking…';
  try {
    const r = await fetch('/api/check_writing', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level, items
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    if (part === 'A') {
      state.writingAAnswers = items.map(it => it.answer);
      state.writingAResults = data.results || [];
    } else {
      state.writingBAnswers = items.map(it => it.answer);
      state.writingBResults = data.results || [];
    }
    renderWritingFeedback(part, data.results || []);
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = orig; }
}

function renderWritingFeedback(part, results) {
  const container = part === 'A' ? $('writingAForm') : $('writingBForm');
  const blocks = container.querySelectorAll('.writing-q');
  let total = 0, n = 0;
  results.forEach(r => {
    const b = blocks[r.index]; if (!b) return;
    const fb = b.querySelector('.feedback');
    fb.classList.remove('hidden');
    const errs = (r.errors || []).map(e => '<li>' + escapeHtml(e) + '</li>').join('');
    fb.innerHTML =
      '<div class="fb-score">Score: ' + (r.score != null ? r.score : '?') + '/100</div>' +
      '<div class="fb-corrected"><strong>Corrected:</strong> ' + escapeHtml(r.corrected) + '</div>' +
      (errs ? '<div class="fb-errors"><strong>Errors:</strong><ul>' + errs + '</ul></div>' : '') +
      '<div class="fb-comment">' + escapeHtml(r.feedback || '') + '</div>';
    total += (r.score || 0);
    n++;
  });
  const avg = n ? Math.round(total / n) : 0;
  (part === 'A' ? $('scoreWA') : $('scoreWB')).textContent = 'Average: ' + avg + '%';
  (part === 'A' ? $('btnToWritingB') : $('btnToDictation')).classList.remove('hidden');
}

// ---------------------------------------------------------- writing B
async function goWritingB() {
  showView('writingB');
  if (state.writingB.length) { renderWritingB(); return; }
  $('writingBLoading').classList.remove('hidden');
  try {
    const r = await fetch('/api/writing_b', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level, text: state.adapted.text
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.writingB = data.items || [];
    renderWritingB();
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('writingBLoading').classList.add('hidden'); }
}

function renderWritingB() {
  const form = $('writingBForm');
  form.innerHTML = '';
  state.writingB.forEach((it, i) => {
    const block = document.createElement('div');
    block.className = 'writing-q';
    block.innerHTML =
      '<div class="q-head"><span class="q-num">' + (i + 1) + '.</span>' +
      '<span class="q-text">' + escapeHtml(it.structure) + '</span>' +
      '<button type="button" class="speak-btn">🔊</button></div>' +
      '<small class="hint">Meaning: ' + escapeHtml(it.meaning) +
      ' — Example: <em>' + escapeHtml(it.example) + '</em></small>' +
      '<textarea rows="2" placeholder="Écris ta propre phrase…"></textarea>' +
      '<div class="feedback hidden"></div>';
    block.querySelector('.speak-btn').onclick = () =>
      speak(it.structure, RATE_BY_LEVEL[state.level]);
    const ta = block.querySelector('textarea');
    ta.value = state.writingBAnswers[i] || '';
    ta.oninput = () => { state.writingBAnswers[i] = ta.value; };
    form.appendChild(block);
  });
  if (state.writingBResults.length === state.writingB.length && state.writingB.length) {
    renderWritingFeedback('B', state.writingBResults);
  }
}

// ---------------------------------------------------------- dictation
async function goDictation() {
  showView('dictation');
  if (state.dictation.length) { renderDictation(); return; }
  $('dictLoading').classList.remove('hidden');
  try {
    const r = await fetch('/api/dictation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level, text: state.adapted.text
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.dictation = data.sentences || [];
    renderDictation();
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('dictLoading').classList.add('hidden'); }
}

function renderDictation() {
  const form = $('dictForm');
  form.innerHTML = '';
  state.dictation.forEach((s, i) => {
    const block = document.createElement('div');
    block.className = 'writing-q';
    block.innerHTML =
      '<div class="q-head"><span class="q-num">' + (i + 1) + '.</span>' +
      '<button type="button" class="play-btn small normal">🔊 Play</button> ' +
      '<button type="button" class="play-btn small slow">🐢 Slow</button></div>' +
      '<textarea rows="2" placeholder="Écris ce que tu entends…"></textarea>' +
      '<div class="feedback hidden"></div>';
    block.querySelector('.normal').onclick = () =>
      speak(s, RATE_BY_LEVEL[state.level]);
    block.querySelector('.slow').onclick = () => speak(s, '-40%');
    const ta = block.querySelector('textarea');
    ta.value = state.dictationAnswers[i] || '';
    ta.oninput = () => { state.dictationAnswers[i] = ta.value; };
    form.appendChild(block);
  });
  if (state.dictationResults.length === state.dictation.length && state.dictation.length) {
    renderDictationFeedback(state.dictationResults);
  }
}

async function checkDict() {
  const form = $('dictForm');
  const items = [];
  form.querySelectorAll('textarea').forEach((ta, i) => {
    items.push({ index: i, expected: state.dictation[i], answer: ta.value.trim() });
  });
  $('btnCheckDict').disabled = true;
  $('btnCheckDict').textContent = 'Checking…';
  try {
    const r = await fetch('/api/check_dictation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level, items
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.dictationAnswers = items.map(it => it.answer);
    state.dictationResults = data.results || [];
    renderDictationFeedback(state.dictationResults);
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally {
    $('btnCheckDict').disabled = false;
    $('btnCheckDict').textContent = 'Check my dictation';
  }
}

function renderDictationFeedback(results) {
  const blocks = $('dictForm').querySelectorAll('.writing-q');
  let total = 0, n = 0;
  results.forEach(res => {
    const b = blocks[res.index]; if (!b) return;
    const fb = b.querySelector('.feedback');
    fb.classList.remove('hidden');
    fb.innerHTML =
      '<div class="fb-score">Score: ' + res.score + '/100</div>' +
      '<div class="fb-corrected"><strong>Expected:</strong> ' +
      escapeHtml(res.corrected) + '</div>' +
      '<div class="fb-comment">' + escapeHtml(res.feedback || '') + '</div>';
    total += res.score || 0;
    n++;
  });
  const avg = n ? Math.round(total / n) : 0;
  $('scoreDict').textContent = 'Average: ' + avg + '%';
  $('btnToCheatSheet').classList.remove('hidden');
}

// ---------------------------------------------------------- cheat sheet
async function goCheatSheet() {
  showView('cheatsheet');
  if (state.cheatsheet) { renderCheatSheet(); return; }
  $('cheatLoading').classList.remove('hidden');
  try {
    const r = await fetch('/api/cheatsheet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_key: state.geminiKey, openrouter_key: state.openrouterKey, level: state.level,
        text: state.adapted.text, structures: state.writingB, sentences: state.dictation
      })
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    state.cheatsheet = data;
    renderCheatSheet();
    saveSession();
  } catch (e) { alert('Error: ' + e.message); }
  finally { $('cheatLoading').classList.add('hidden'); }
}

function renderCheatSheet() {
  const c = state.cheatsheet || {};
  const vocab = (c.vocab || []).map(v =>
    '<div class="cheat-vocab-item">' +
    '<span class="cheat-word">' + escapeHtml(v.word) + '</span>' +
    '<span class="cheat-meaning">' + escapeHtml(v.meaning) + '</span>' +
    '<button type="button" class="speak-btn" data-w="' + escapeHtml(v.word) + '">🔊</button>' +
    '</div>'
  ).join('');
  const structs = (c.structures || []).map(s =>
    '<div class="cheat-struct-item">' +
    '<span class="cheat-structure">' + escapeHtml(s.structure) + '</span>' +
    '<span class="cheat-example">' + escapeHtml(s.example) + '</span>' +
    '</div>'
  ).join('');
  const tips = (c.tips || []).map(t => '<li>' + escapeHtml(t) + '</li>').join('');
  $('cheatContent').innerHTML =
    '<div class="cheat-section"><h3>📚 Key vocabulary</h3><div class="cheat-vocab">' + vocab + '</div></div>' +
    '<div class="cheat-section"><h3>🧩 Structures to remember</h3>' + structs + '</div>' +
    (tips ? '<div class="cheat-section"><h3>💡 Quick tips</h3><ul class="cheat-tips">' + tips + '</ul></div>' : '');
  $('cheatContent').querySelectorAll('.speak-btn[data-w]').forEach(btn => {
    btn.onclick = () => speak(btn.dataset.w, RATE_BY_LEVEL[state.level]);
  });
}

// ---------------------------------------------------------- init
window.addEventListener('DOMContentLoaded', () => {
  $('inpGemini').value = localStorage.getItem('geminiKey') || '';
  $('inpNews').value = localStorage.getItem('newsKey') || '';
  $('inpOpenRouter').value = localStorage.getItem('openrouterKey') || '';
  const v = localStorage.getItem('voice');
  if (v) $('inpVoice').value = v;
  state.voice = $('inpVoice').value;
  state.openrouterKey = $('inpOpenRouter').value;

  $('btnSaveKeys').onclick = saveKeys;
  $('btnLoadNews').onclick = loadNews;
  $('btnBackKeys').onclick = () => showView('keys');
  $('btnBackNews').onclick = () => showView('news');
  $('btnAdapt').onclick = adapt;
  $('btnToPart1').onclick = goPart1;
  $('btnCheckP1').onclick = () => checkQuiz('part1');
  $('btnToPart2').onclick = goPart2;
  $('btnCheckP2').onclick = () => checkQuiz('part2');
  $('btnToWritingA').onclick = goWritingA;
  $('btnCheckWA').onclick = () => checkWriting('A');
  $('btnToWritingB').onclick = goWritingB;
  $('btnCheckWB').onclick = () => checkWriting('B');
  $('btnToDictation').onclick = goDictation;
  $('btnCheckDict').onclick = checkDict;
  $('btnToCheatSheet').onclick = goCheatSheet;
  $('btnRestart').onclick = () => location.reload();

  $('btnArchive').onclick = openArchive;
  $('btnCloseArchive').onclick = closeArchive;
  $('archiveOverlay').addEventListener('click', (e) => {
    if (e.target.id === 'archiveOverlay') closeArchive();
  });

  $('btnPlayAdapted').onclick = playArticle;
  $('btnPlayP1').onclick = playArticle;
  $('btnPlayP2').onclick = playArticle;

  $('levelButtons').querySelectorAll('.level-btn').forEach(b => {
    b.onclick = () => {
      $('levelButtons').querySelectorAll('.level-btn').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      state.level = b.dataset.level;
    };
  });

  // Progress chips: clicking an already-unlocked step jumps back/forward to it
  // without re-triggering generation (goX() functions reuse cached state).
  document.querySelectorAll('#progress span').forEach((el, i) => {
    el.onclick = () => {
      if (i > state.maxStepIndex) return; // locked — not reached yet
      const step = el.dataset.step;
      const fn = STEP_GO[step];
      if (fn) fn();
    };
  });

  showView('keys');
});
"""


# ============================================================ Static routes

@app.get("/")
def index():
    index_path = os.path.join(TEMPLATES_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        return Response(html, mimetype="text/html")
    # fallback to old embedded if template missing
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/static/style.css")
def style_css():
    return Response(STYLE_CSS, mimetype="text/css")


@app.get("/static/app.js")
def app_js():
    return Response(APP_JS, mimetype="application/javascript")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # Never expose the Werkzeug debugger (interactive RCE) to the network. Debug is opt-in and
    # we only bind all interfaces on a host that sets PORT-style env (e.g. Render sets RENDER=true).
    debug = os.getenv("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    host = os.getenv("HOST") or ("0.0.0.0" if os.getenv("RENDER") else "127.0.0.1")
    if debug and host != "127.0.0.1":
        print("!! FLASK_DEBUG ignored for non-local host (debugger would be remotely exploitable)")
        debug = False
    print(f"-> open http://127.0.0.1:{port}  Cloudinary={'ON' if USE_CLOUDINARY else 'OFF'}")
    app.run(host=host, port=port, debug=debug)
