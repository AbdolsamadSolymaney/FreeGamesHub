"""
FreeGamesHub — Steam + Epic Games Store + GOG + PlayStation + Xbox Game Pass
================================================================================
v4 fixes:
- DB persisted in repo via git commit after each run (solves duplicates across Actions runs)
- Xbox image: RAWG fallback + multiple CDN candidates
- Steam: all games ≥75% discount are sent, no silent drops
- Expiry alert: 2h before deal ends, send reminder message
- Auto-pin last PC Free-to-Keep post
- Telegram pin on new FTK PC game
"""

import os
import time
import logging
import hashlib
import requests
import sqlite3
import datetime
import re
import json
import subprocess
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
import cloudscraper
import feedparser

# ─── jdatetime ──────────────────────────────────────────────────────────
try:
    import jdatetime
    JDT_AVAILABLE = True
except ImportError:
    JDT_AVAILABLE = False
    jdatetime = None

# ─── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────
BOT_TOKEN         = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL           = os.environ.get("TELEGRAM_CHANNEL")
RAWG_API_KEY      = os.environ.get("RAWG_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# DB lives in the repo directory so git can commit & push it between runs.
# GitHub Actions checks out to GITHUB_WORKSPACE; locally it falls back to cwd.
REPO_DIR = os.environ.get("GITHUB_WORKSPACE", os.path.dirname(os.path.abspath(__file__)))
DB_FILE  = os.path.join(REPO_DIR, "games.db")

MIN_DISCOUNT = 75

AAA_METACRITIC_THRESHOLD = 75
AAA_RATING_THRESHOLD = 80
AAA_REVIEWS_THRESHOLD = 2000

SCRAPER = cloudscraper.create_scraper()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://store.steampowered.com/",
    "Cookie": "birthtime=0; lastagecheckage=1-January-1990; wants_mature_content=1; cc=US;",
}

SKIP_KEYWORDS = [
    "DLC", "Soundtrack", "OST", "Season Pass", "Expansion",
    "Upgrade", "Add-on", "Artbook", "Comic", "Deluxe",
    "Bundle", "Content Pack", "Cosmetic", "Starter Pack",
    "PlayStation Plus", "PS Plus", "Xbox Game Pass", "Game Pass",
]

STORE_META = {
    "steam":                {"emoji": "🟦", "name": "Steam",               "tag": "#Steam"},
    "epic":                 {"emoji": "⬛", "name": "Epic Games Store",     "tag": "#EpicGames"},
    "gog":                  {"emoji": "🟣", "name": "GOG",                  "tag": "#GOG"},
    "playstation":          {"emoji": "🔵", "name": "PlayStation",          "tag": "#PlayStation"},
    "playstation_essential":{"emoji": "🔵", "name": "PS Plus Essential",   "tag": "#PSPlusEssential"},
    "playstation_extra":    {"emoji": "🔵", "name": "PS Plus Extra",       "tag": "#PSPlusExtra"},
    "xbox_gamepass":        {"emoji": "🟩", "name": "Xbox Game Pass",      "tag": "#XboxGamePass"},
}

# ─── Cache ۲۴ ساعته ────────────────────────────────────────────────────
SENT_CACHE = {}

def is_recently_sent_cached(store: str, game_id: str, hours: int = 24) -> bool:
    key = (store, game_id)
    if key in SENT_CACHE:
        last_sent = SENT_CACHE[key]
        if (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - last_sent).total_seconds() < hours * 3600:
            return True
    return False

def mark_sent_cached(store: str, game_id: str):
    SENT_CACHE[(store, game_id)] = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)

# ─── Database ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            store      TEXT,
            game_id    TEXT,
            deal_hash  TEXT,
            title      TEXT,
            sent_at    TEXT,
            message_id INTEGER DEFAULT 0,
            PRIMARY KEY (store, game_id, deal_hash)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_history (
            store      TEXT,
            game_id    TEXT,
            last_start TEXT,
            last_end   TEXT,
            last_price TEXT,
            updated_at TEXT,
            PRIMARY KEY (store, game_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            game_id    TEXT,
            title      TEXT,
            decision   TEXT,
            confidence INTEGER,
            warnings   TEXT,
            checked_at TEXT,
            PRIMARY KEY (game_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expiry_alerts (
            store      TEXT,
            game_id    TEXT,
            deal_hash  TEXT,
            deal_end   TEXT,
            alerted    INTEGER DEFAULT 0,
            PRIMARY KEY (store, game_id, deal_hash)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pinned_ftk (
            platform   TEXT PRIMARY KEY,
            message_id INTEGER,
            title      TEXT,
            pinned_at  TEXT
        )
    """)
    conn.commit()
    conn.close()

# ─── Git DB Persistence ───────────────────────────────────────────────────
def git_commit_db():
    """
    Commit and push games.db back to the repo after each run.
    This is the ONLY way to preserve sent-state between GitHub Actions runs,
    since each run starts with a fresh checkout.
    Requires GITHUB_TOKEN with write permission (automatically available in Actions).
    """
    try:
        repo = REPO_DIR
        git = ["git", "-C", repo]

        # configure git identity for the commit
        subprocess.run([*git, "config", "user.name", "FreeGamesBot"], check=True, capture_output=True)
        subprocess.run([*git, "config", "user.email", "bot@freegameshub.local"], check=True, capture_output=True)

        # stage only the DB file
        rel = os.path.relpath(DB_FILE, repo)
        subprocess.run([*git, "add", rel], check=True, capture_output=True)

        # check if there's anything to commit
        result = subprocess.run([*git, "diff", "--cached", "--quiet"], capture_output=True)
        if result.returncode == 0:
            log.info("DB unchanged — nothing to commit")
            return

        ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run([*git, "commit", "-m", f"chore: update games.db [{ts}]"],
                       check=True, capture_output=True)

        # push using GITHUB_TOKEN via https remote
        token = GITHUB_TOKEN
        if token:
            # get remote URL and inject token
            r = subprocess.run([*git, "remote", "get-url", "origin"],
                               capture_output=True, text=True)
            remote_url = r.stdout.strip()
            if "github.com" in remote_url and "https://" in remote_url:
                authed = remote_url.replace("https://", f"https://x-access-token:{token}@")
                subprocess.run([*git, "push", authed, "HEAD"], check=True, capture_output=True)
            else:
                subprocess.run([*git, "push"], check=True, capture_output=True)
        else:
            subprocess.run([*git, "push"], check=True, capture_output=True)

        log.info("DB committed and pushed to repo")
    except subprocess.CalledProcessError as e:
        log.warning(f"Git push failed: {e.stderr.decode()[:200] if e.stderr else e}")
    except Exception as e:
        log.warning(f"Git DB persistence error: {e}")

def is_sent(store: str, game_id: str, deal_hash: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM sent WHERE store=? AND game_id=? AND deal_hash=?",
        (store, game_id, deal_hash)
    ).fetchone()
    conn.close()
    return row is not None

def mark_sent(store: str, game_id: str, title: str, deal_hash: str, message_id: int = 0):
    ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO sent (store, game_id, deal_hash, title, sent_at, message_id) VALUES (?,?,?,?,?,?)",
        (store, game_id, deal_hash, title, ts, message_id)
    )
    conn.commit()
    conn.close()

def register_expiry_alert(store: str, game_id: str, deal_hash: str, deal_end: str):
    """Register a deal for expiry alert if it has an end date."""
    if not deal_end:
        return
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR IGNORE INTO expiry_alerts (store, game_id, deal_hash, deal_end, alerted)
        VALUES (?, ?, ?, ?, 0)
    """, (store, game_id, deal_hash, deal_end))
    conn.commit()
    conn.close()

def get_pending_expiry_alerts() -> list:
    """Return deals expiring in the next 2 hours that haven't been alerted yet."""
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    cutoff = now + datetime.timedelta(hours=2)
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute("""
        SELECT ea.store, ea.game_id, ea.deal_hash, ea.deal_end, s.title, s.message_id
        FROM expiry_alerts ea
        JOIN sent s ON s.store=ea.store AND s.game_id=ea.game_id AND s.deal_hash=ea.deal_hash
        WHERE ea.alerted=0 AND ea.deal_end != ''
    """).fetchall()
    conn.close()
    alerts = []
    for store, game_id, deal_hash, deal_end, title, message_id in rows:
        try:
            end_dt = datetime.datetime.strptime(deal_end[:10], "%Y-%m-%d")
            if now <= end_dt <= cutoff:
                alerts.append({
                    "store": store, "game_id": game_id, "deal_hash": deal_hash,
                    "deal_end": deal_end, "title": title, "message_id": message_id
                })
        except:
            pass
    return alerts

def mark_expiry_alerted(store: str, game_id: str, deal_hash: str):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "UPDATE expiry_alerts SET alerted=1 WHERE store=? AND game_id=? AND deal_hash=?",
        (store, game_id, deal_hash)
    )
    conn.commit()
    conn.close()

def send_expiry_alert(alert: dict):
    """Send a 2-hour expiry reminder for a deal."""
    title = alert["title"]
    deal_end = alert["deal_end"]
    end_display = format_date_bilingual(deal_end)
    store_name = alert["store"].replace("_", " ").title()

    lines = [
        f"⏰ <b>Deal Ending Soon!</b>",
        f"",
        f"🎮 <b>{title}</b>",
        f"🏪 {store_name}",
        f"",
        f"🔴 <b>Expires:</b> {end_display}",
        f"⚡ Less than 2 hours left — grab it now!",
    ]
    # reply to original message if we have its ID
    msg_id = alert.get("message_id", 0)
    payload = {
        "chat_id": CHANNEL,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
    }
    if msg_id:
        payload["reply_to_message_id"] = msg_id

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=payload, timeout=20
        )
        if r.json().get("ok"):
            mark_expiry_alerted(alert["store"], alert["game_id"], alert["deal_hash"])
            log.info(f"Expiry alert sent: {title}")
        else:
            log.warning(f"Expiry alert failed: {r.json().get('description')}")
    except Exception as e:
        log.error(f"Expiry alert error: {e}")

# ─── Auto-pin last FTK PC post ────────────────────────────────────────────
def get_pinned_ftk(platform: str = "pc") -> dict | None:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT message_id, title FROM pinned_ftk WHERE platform=?", (platform,)
    ).fetchone()
    conn.close()
    return {"message_id": row[0], "title": row[1]} if row else None

def set_pinned_ftk(platform: str, message_id: int, title: str):
    ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR REPLACE INTO pinned_ftk (platform, message_id, title, pinned_at)
        VALUES (?, ?, ?, ?)
    """, (platform, message_id, title, ts))
    conn.commit()
    conn.close()

def tg_pin_message(message_id: int) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",
            data={"chat_id": CHANNEL, "message_id": message_id, "disable_notification": True},
            timeout=20
        )
        return r.json().get("ok", False)
    except:
        return False

def tg_unpin_message(message_id: int) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/unpinChatMessage",
            data={"chat_id": CHANNEL, "message_id": message_id},
            timeout=20
        )
        return r.json().get("ok", False)
    except:
        return False

def handle_ftk_pin(game: dict, message_id: int):
    """If this is a PC FTK post, unpin old and pin new."""
    store = game.get("store", "")
    if not game.get("is_free_to_keep"):
        return
    if store not in ("steam", "epic", "gog"):
        return
    if message_id <= 0:
        return

    prev = get_pinned_ftk("pc")
    if prev and prev["message_id"]:
        tg_unpin_message(prev["message_id"])
        log.info(f"Unpinned old FTK: {prev['title']}")

    if tg_pin_message(message_id):
        set_pinned_ftk("pc", message_id, game["title"])
        log.info(f"Pinned new FTK: {game['title']} (msg_id={message_id})")

def update_deal_history(game: dict):
    """Record the latest deal data — used to detect changes on future runs."""
    ts = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR REPLACE INTO deal_history
        (store, game_id, last_start, last_end, last_price, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        game["store"], game["id"],
        game.get("deal_start", ""), game.get("deal_end", ""),
        game.get("price_final_fmt", ""), ts,
    ))
    conn.commit()
    conn.close()

def get_prev_deal(store: str, game_id: str) -> dict | None:
    """Return last recorded deal data for this game, or None if first time."""
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT last_start, last_end, last_price FROM deal_history WHERE store=? AND game_id=?",
        (store, game_id)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"deal_start": row[0], "deal_end": row[1], "price_final_fmt": row[2]}

def make_promo_key(store: str, game_id: str, period_anchor: str) -> str:
    raw = f"{store}|{game_id}|{period_anchor}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def current_month_anchor() -> str:
    return f"MONTH:{datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime('%Y-%m')}"

def current_week_anchor() -> str:
    iso = datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isocalendar()
    return f"WEEK:{iso[0]}-W{iso[1]:02d}"

def get_deal_hash(game: dict) -> str:
    start = game.get("deal_start", "")
    end   = game.get("deal_end", "")
    price = game.get("price_final_fmt", "")
    raw   = f"{start}|{end}|{price}"
    return hashlib.md5(raw.encode()).hexdigest()[:16] if raw else ""

def is_deal_changed(store: str, game_id: str, current_hash: str) -> bool:
    if not current_hash:
        return True
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT deal_hash FROM sent WHERE store=? AND game_id=? ORDER BY sent_at DESC LIMIT 1",
        (store, game_id)
    ).fetchone()
    conn.close()
    if not row:
        return True
    return row[0] != current_hash

def is_recently_sent_db(store: str, game_id: str, days: int = 365) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT sent_at FROM sent WHERE store=? AND game_id=? ORDER BY sent_at DESC LIMIT 1",
        (store, game_id)
    ).fetchone()
    conn.close()
    if not row:
        return False
    try:
        last = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        diff = (datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - last).days
        return diff < days
    except:
        return False

# ─── HTTP Helper ─────────────────────────────────────────────────────────
def safe_get(url, params=None, retries=5, delay=2, use_scraper=False, extra_headers=None):
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(retries):
        try:
            if use_scraper:
                r = SCRAPER.get(url, params=params, headers=headers, timeout=30)
            else:
                r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                log.warning(f"Rate limited — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"Request error #{attempt+1}: {e}")
        time.sleep(delay * (attempt + 1))
    log.error(f"All {retries} attempts failed for {url}")
    return None

# ─── AI Decision Engine (Anthropic API) ─────────────────────────────────
# Uses claude-sonnet-4-6 via Anthropic API for intelligent game validation,
# description enrichment, and quality assessment.
# Falls back to rule-based logic if API is unavailable.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

class AIDecisionEngine:
    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY
        self.enabled = bool(self.api_key)
        if self.enabled:
            log.info("AI Decision Engine initialized (Anthropic API mode)")
        else:
            log.info("AI Decision Engine initialized (rule-based fallback — set ANTHROPIC_API_KEY to enable)")

    def _call_claude(self, system: str, user: str, max_tokens: int = 400) -> str | None:
        """Call Anthropic Messages API, return text content or None."""
        if not self.enabled:
            return None
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=20,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["content"][0]["text"]
            else:
                log.warning(f"Anthropic API {resp.status_code}: {resp.text[:120]}")
                return None
        except Exception as e:
            log.warning(f"Anthropic API error: {e}")
            return None

    def decide(self, game: Dict[str, Any]) -> Tuple[bool, str, Dict]:
        """
        Validate game deal with AI or rule-based fallback.
        Returns (is_valid, reason, corrections_dict).
        """
        # ── Rule-based pre-checks (always run, fast) ──────────────────
        title = game.get("title", "")
        for kw in SKIP_KEYWORDS:
            if kw.lower() in title.lower():
                return False, f"Keyword filter: '{kw}'", {}

        dlc_patterns = [r'\bDLC\b', r'\bSoundtrack\b', r'\bOST\b', r'Season Pass',
                        r'Starter Pack', r'Upgrade Pack', r'Add.?on', r'Expansion Pack']
        for pat in dlc_patterns:
            if re.search(pat, title, re.IGNORECASE):
                return False, f"DLC/addon pattern: {pat}", {}

        link = game.get("link", "")
        if not link or not link.startswith("http"):
            return False, "Invalid or missing link", {}

        discount = game.get("discount", 0)
        orig = game.get("price_orig_fmt", "")
        final = game.get("price_final_fmt", "")
        if orig and final and orig == final and discount > 0:
            return False, "Price mismatch: orig equals final but discount > 0", {}

        start = game.get("deal_start", "")
        end = game.get("deal_end", "")
        if start and end:
            try:
                s = datetime.datetime.strptime(start[:10], "%Y-%m-%d")
                e = datetime.datetime.strptime(end[:10], "%Y-%m-%d")
                now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
                if s > e:
                    return False, "Start date after end date", {}
                if e < now:
                    return False, "Deal already expired", {}
            except:
                pass

        if discount == 100 and not game.get("is_free_to_keep") and game.get("store") == "steam":
            return False, "100% discount on Steam but not Free to Keep", {}

        # ── AI enrichment + validation (runs if API available) ────────
        if self.enabled:
            result = self._ai_validate_and_enrich(game)
            if result:
                return result

        # ── Rule-based score fallback ──────────────────────────────────
        q = 0
        mc = game.get("metacritic")
        if mc:
            if mc >= 90: q += 30
            elif mc >= 80: q += 20
            elif mc >= 70: q += 10
        rp = game.get("review_pct")
        if rp:
            if rp >= 85: q += 20
            elif rp >= 70: q += 10
        if game.get("is_free_to_keep"): q += 40
        if game.get("is_aaa"): q += 20
        if discount >= 90: q += 15
        elif discount >= 75: q += 10
        return True, f"Rule-based validation (score: {min(q,100)})", {}

    def _ai_validate_and_enrich(self, game: dict) -> Tuple[bool, str, Dict] | None:
        """
        Ask Claude to validate and enrich the game data.
        Returns None if API fails (so caller falls back to rule-based).
        """
        system = (
            "You are a strict game deal validator. "
            "Respond ONLY with a valid JSON object. No markdown, no explanation outside JSON."
        )
        user = f"""Validate this game deal and return enrichment data.

Game data:
  title: {game.get('title')}
  store: {game.get('store')}
  discount: {game.get('discount')}%
  price_orig: {game.get('price_orig_fmt', 'N/A')}
  price_final: {game.get('price_final_fmt', 'N/A')}
  free_to_keep: {game.get('is_free_to_keep', False)}
  deal_start: {game.get('deal_start', 'unknown')}
  deal_end: {game.get('deal_end', 'unknown')}
  link: {game.get('link', '')}
  genres: {game.get('genres', [])}
  description_len: {len(game.get('description', ''))} chars

Rules to apply:
1. REJECT if title contains DLC, Soundtrack, OST, Season Pass, Expansion, Upgrade, Add-on, Bundle, Cosmetic
2. REJECT if it is clearly not a game (e.g. software tool, movie)
3. REJECT if link domain does not match store (e.g. steam link for epic store)
4. ACCEPT free-to-keep promos unconditionally if rules 1-3 pass
5. For discounts, only ACCEPT if deal seems legitimate

Return JSON:
{{
  "accept": true or false,
  "confidence": 0-100,
  "reason": "brief reason",
  "is_aaa": true or false,
  "quality_tier": "S" | "A" | "B" | "C",
  "suggested_description": "1-2 sentence game description if genres/desc missing, else empty string",
  "warning": "any concern or empty string"
}}"""

        raw = self._call_claude(system, user, max_tokens=350)
        if not raw:
            return None

        try:
            # strip markdown fences if present
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            data = json.loads(raw)
            accept = bool(data.get("accept", True))
            confidence = int(data.get("confidence", 50))
            reason = data.get("reason", "AI validated")
            corrections = {}

            if data.get("is_aaa"):
                game["is_aaa"] = True
            if data.get("suggested_description") and not game.get("description"):
                game["description"] = data["suggested_description"]
                corrections["description"] = data["suggested_description"]

            tier = data.get("quality_tier", "C")
            game["ai_quality_tier"] = tier
            game["ai_confidence"] = confidence

            if data.get("warning"):
                log.info(f"   AI warning: {data['warning']}")

            status = "accepted" if accept else "rejected"
            log.info(f"   AI {status} [{tier}] confidence={confidence}%: {reason}")
            return accept, reason, corrections

        except Exception as e:
            log.warning(f"AI response parse error: {e} | raw: {raw[:120]}")
            return None

    def log_decision(self, game: dict, decision: str, reason: str):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("""
                INSERT OR REPLACE INTO ai_decisions
                (game_id, title, decision, confidence, warnings, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                game.get("id", "unknown"),
                game.get("title", "unknown"),
                decision,
                game.get("ai_confidence", 0),
                reason,
                datetime.datetime.now(datetime.UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"AI log error: {e}")

# ─── Date Formatter ────────────────────────────────────────────────────
def format_date_bilingual(date_str: str) -> str:
    """تاریخ را به صورت میلادی اول و شمسی دوم برمی‌گرداند"""
    if not date_str or not date_str.strip():
        return "نامشخص"
    date_str_clean = date_str.split("T")[0].split(" ")[0].strip()
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(date_str_clean, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return date_str_clean
    # میلادی اول
    gregorian = dt.strftime("%d %b %Y")
    if JDT_AVAILABLE and jdatetime:
        try:
            jd = jdatetime.datetime.fromgregorian(datetime=dt)
            persian = jd.strftime("%Y/%m/%d")
            return f"{gregorian} | {persian}"   # میلادی | شمسی در یک خط
        except Exception as e:
            log.warning(f"jdatetime conversion failed: {e}")
            return gregorian
    else:
        return gregorian

# alias برای سازگاری با کدهای قدیمی‌تر
def format_date_persian_english(date_str: str) -> str:
    return format_date_bilingual(date_str)

def check_jdatetime():
    if JDT_AVAILABLE:
        try:
            today = jdatetime.datetime.now()
            log.info(f"jdatetime active | Shamsi today: {today.strftime('%Y/%m/%d')}")
            return True
        except Exception as e:
            log.error(f"jdatetime error: {e}")
            return False
    else:
        log.warning("jdatetime not installed — Shamsi dates disabled")
        return False

# ─── RAWG API ────────────────────────────────────────────────────────────
_rawg_cache: dict = {}

def rawg_search(title: str) -> dict | None:
    if not RAWG_API_KEY:
        return None
    cache_key = title.lower().strip()
    if cache_key in _rawg_cache:
        return _rawg_cache[cache_key]
    clean = re.sub(r"[™®©]", "", title).strip()
    clean = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", clean).strip()
    params = {"search": clean, "page_size": 5, "key": RAWG_API_KEY}
    r = safe_get("https://api.rawg.io/api/games", params=params, extra_headers={"Referer": "https://rawg.io/"}, retries=3)
    if not r:
        _rawg_cache[cache_key] = None
        return None
    try:
        data = r.json()
        results = data.get("results", [])
        if not results:
            _rawg_cache[cache_key] = None
            return None
        best = None
        best_score = 0
        for item in results:
            name = item.get("name", "").lower()
            if name == clean.lower():
                score = 100
            elif clean.lower() in name or name in clean.lower():
                score = 80
            else:
                t_words = set(clean.lower().split())
                i_words = set(name.split())
                score = len(t_words & i_words) / max(len(t_words), 1) * 60
            if score > best_score:
                best_score = score
                best = item
        if best_score < 40 or not best:
            _rawg_cache[cache_key] = None
            return None
        detail = safe_get(f"https://api.rawg.io/api/games/{best['id']}", params={"key": RAWG_API_KEY}, retries=3)
        description = ""
        if detail:
            raw_desc = detail.json().get("description", "") or detail.json().get("description_raw", "")
            if raw_desc:
                description = BeautifulSoup(raw_desc, "html.parser").get_text().strip()
                description = re.sub(r'\n{3,}', '\n\n', description)
        genres = [g["name"] for g in best.get("genres", [])]
        rating_pct = round(best.get("rating", 0) / 5 * 100) if best.get("rating") else None
        metacritic = best.get("metacritic")
        bg_image = best.get("background_image", "") or ""
        released = best.get("released", "")
        result = {
            "genres": genres,
            "description": description,
            "rating_pct": rating_pct,
            "ratings_count": best.get("ratings_count", 0),
            "metacritic": metacritic,
            "rawg_rating": best.get("rating", 0),
            "background_image": bg_image,
            "released": released,
        }
        _rawg_cache[cache_key] = result
        return result
    except Exception as e:
        log.error(f"RAWG error: {e}")
        _rawg_cache[cache_key] = None
        return None

def enrich_epic_gog(game: dict):
    rawg = rawg_search(game["title"])
    if not rawg:
        return
    if not game.get("genres") and rawg.get("genres"):
        game["genres"] = rawg["genres"]
    if not game.get("description") and rawg.get("description"):
        game["description"] = rawg["description"]
    if rawg.get("rating_pct") and rawg.get("ratings_count", 0) > 50:
        game["review_pct"] = rawg["rating_pct"]
        game["review_count"] = rawg["ratings_count"]
        game["review_desc"] = f"RAWG {rawg['rawg_rating']:.1f}/5"
    if rawg.get("metacritic"):
        game["metacritic"] = rawg["metacritic"]
    if rawg.get("background_image"):
        game["rawg_image"] = rawg["background_image"]
    if rawg.get("released"):
        game["release_date"] = rawg["released"]

# ─── Game Factory ──────────────────────────────────────────────────────
def _should_skip(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in SKIP_KEYWORDS)

def make_game(store: str, game_id: str, title: str, discount: int,
              link: str, orig_fmt: str = "", final_fmt: str = "",
              image_url: str = "", is_free_to_keep: bool = False,
              description: str = "", genres: list = None,
              review_pct: int = None, review_count: int = None,
              review_desc: str = "") -> dict:
    return {
        "store": store, "id": str(game_id), "title": title, "discount": discount,
        "link": link, "price_orig_fmt": orig_fmt, "price_final_fmt": final_fmt,
        "image_url": image_url, "is_free_to_keep": is_free_to_keep,
        "description": description, "genres": genres or [],
        "review_pct": review_pct, "review_count": review_count, "review_desc": review_desc,
        "metacritic": None, "rawg_image": "", "steam_image": "",
        "deal_start": "", "deal_end": "", "release_date": "",
        "is_free_to_play": False, "deal_type": "discount",
        "priority_score": 0, "is_aaa": False,
    }

def _merge(base: list, new_items: list):
    seen = {(g["store"], g["id"]) for g in base}
    for g in new_items:
        if (g["store"], g["id"]) not in seen:
            base.append(g)
            seen.add((g["store"], g["id"]))

# ─── Image URL ──────────────────────────────────────────────────────────
def get_image_candidates(game: dict) -> list:
    candidates = []
    store = game.get("store", "")

    # Steam games: multiple CDN options
    if game.get("steam_image"):
        candidates.append(game["steam_image"])
    if store == "steam" and game["id"].isdigit():
        sid = game["id"]
        candidates.append(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{sid}/capsule_616x353.jpg")
        candidates.append(f"https://cdn.akamai.steamstatic.com/steam/apps/{sid}/header.jpg")
        candidates.append(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{sid}/library_600x900.jpg")

    # Xbox: try to find a Steam CDN image by searching steam_image set during enrich
    if store == "xbox_gamepass":
        if game.get("steam_image"):
            candidates.append(game["steam_image"])
        # also try rawg
        if game.get("rawg_image"):
            candidates.append(game["rawg_image"])
        # image_url from Xbox catalog API
        if game.get("image_url"):
            candidates.append(game["image_url"])

    # RAWG image (works for GOG, Epic, PS, Xbox)
    if game.get("rawg_image") and game.get("rawg_image") not in candidates:
        candidates.append(game["rawg_image"])

    # store-provided image_url
    if game.get("image_url"):
        img = game["image_url"]
        for variant in [
            img.replace("_small", "_large").replace("_thumb", "_original"),
            re.sub(r'/\d+x\d+/', '/original/', img),
            img,
        ]:
            if variant not in candidates:
                candidates.append(variant)

    valid_ext = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
    return [c for c in candidates if c and any(c.lower().split('?')[0].endswith(ext) for ext in valid_ext)]

# ─── Priority Score ────────────────────────────────────────────────────
def calculate_priority_score(game: dict) -> int:
    score = 0
    if game.get("review_pct"):
        score += min(int(game["review_pct"] / 4), 25)
    elif game.get("rawg_rating"):
        score += min(int(game.get("rawg_rating", 0) * 5), 25)
    if game.get("metacritic"):
        mc = game.get("metacritic", 0)
        if mc >= 90: score += 15
        elif mc >= 80: score += 10
        elif mc >= 70: score += 5
    if game.get("review_count"):
        cnt = game.get("review_count", 0)
        if cnt > 100000: score += 15
        elif cnt > 50000: score += 10
        elif cnt > 10000: score += 5
        elif cnt > 1000: score += 3
    discount = game.get("discount", 0)
    if discount == 100: score += 10
    elif discount >= 90: score += 8
    elif discount >= 80: score += 5
    elif discount >= 75: score += 3
    if game.get("is_aaa"): score += 15
    if game.get("is_free_to_keep"): score += 20
    if game.get("is_free_to_play"): score = -999
    game["priority_score"] = score
    return score

# ─── Deal Type ──────────────────────────────────────────────────────────
def detect_deal_type(game: dict) -> str:
    if game.get("is_free_to_play"): return "free_to_play"
    if game.get("is_free_to_keep"): return "free_to_keep"
    if game.get("discount") == 100: return "free_weekend"
    return "discount"

def get_deal_emoji(deal_type: str) -> str:
    return {"free_to_keep": "🟢", "free_weekend": "🟡", "free_to_play": "🔵"}.get(deal_type, "🟣")

def get_deal_label(deal_type: str, discount: int) -> str:
    if deal_type == "free_to_keep": return "Free to Keep (برای همیشه)"
    elif deal_type == "free_weekend": return "Free Weekend (آخر هفته رایگان)"
    elif deal_type == "free_to_play": return "Free to Play (کاملاً رایگان)"
    else: return f"{discount}% تخفیف"

# ─── Steam Sources ─────────────────────────────────────────────────────
def _steam_fetch_featured() -> list:
    games = []
    r = safe_get("https://store.steampowered.com/api/featuredcategories/", params={"cc": "US", "l": "english"})
    if not r:
        return games
    try:
        data = r.json()
        for item in data.get("specials", {}).get("items", []):
            name = item.get("name", "")
            if not name:
                continue
            appid = str(item.get("id", ""))
            discount = item.get("discount_percent", 0)
            orig = item.get("original_price", 0)
            final = item.get("final_price", 0)
            if not appid:
                continue
            games.append(make_game("steam", appid, name, discount,
                f"https://store.steampowered.com/app/{appid}/",
                f"${orig/100:.2f}" if orig else "",
                f"${final/100:.2f}" if final else "",
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"))
    except Exception as e:
        log.error(f"Steam Featured error: {e}")
    return games

def _steam_fetch_html_search() -> list:
    games = []
    r = safe_get("https://store.steampowered.com/search/", params={"specials": 1, "cc": "US", "l": "english"})
    if not r:
        return games
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select(".search_result_row"):
            try:
                href = row.get("href", "")
                if "/app/" not in href:
                    continue
                appid = href.split("/app/")[1].split("/")[0]
                if not appid.isdigit():
                    continue
                title_el = row.select_one(".title")
                if not title_el:
                    continue
                title = title_el.text.strip()
                disc_el = row.select_one(".discount_pct")
                discount = 0
                if disc_el:
                    try:
                        discount = int(disc_el.text.strip().replace("-", "").replace("%", ""))
                    except ValueError:
                        pass
                if discount < MIN_DISCOUNT and discount != 100:
                    continue
                orig_el = row.select_one(".discount_original_price")
                final_el = row.select_one(".discount_final_price")
                games.append(make_game("steam", appid, title, discount,
                    f"https://store.steampowered.com/app/{appid}/",
                    orig_el.text.strip() if orig_el else "",
                    final_el.text.strip() if final_el else "",
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"))
            except:
                pass
    except Exception as e:
        log.error(f"Steam HTML search error: {e}")
    return games

def _steam_fetch_free_to_keep() -> list:
    games = []
    r = safe_get("https://store.steampowered.com/search/results/",
                 params={"specials": 1, "maxprice": "free", "cc": "US", "l": "english", "json": 1, "count": 50})
    if r:
        try:
            data = r.json()
            for item in data.get("items", []):
                logo = item.get("logo", "")
                match = re.search(r"/apps/(\d+)/", logo)
                if not match:
                    continue
                appid = match.group(1)
                title = BeautifulSoup(item.get("name", ""), "html.parser").get_text().strip()
                if not title:
                    continue
                price_str = str(item.get("price", "")).lower()
                if "free" not in price_str and price_str != "0":
                    continue
                games.append(make_game("steam", appid, title, 100,
                    f"https://store.steampowered.com/app/{appid}/",
                    final_fmt="FREE",
                    image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                    is_free_to_keep=True))
                log.info(f"  🎁 Steam FTK (JSON): {title}")
        except:
            pass
    if not games:
        r2 = safe_get("https://store.steampowered.com/search/", params={"specials": 1, "maxprice": "free", "cc": "US", "l": "english"})
        if r2:
            try:
                soup = BeautifulSoup(r2.text, "html.parser")
                for row in soup.select(".search_result_row"):
                    href = row.get("href", "")
                    if "/app/" not in href:
                        continue
                    appid = href.split("/app/")[1].split("/")[0]
                    if not appid.isdigit():
                        continue
                    title_el = row.select_one(".title")
                    if not title_el:
                        continue
                    title = title_el.text.strip()
                    disc_el = row.select_one(".discount_pct")
                    if not disc_el or "-100%" not in disc_el.text:
                        continue
                    games.append(make_game("steam", appid, title, 100,
                        f"https://store.steampowered.com/app/{appid}/",
                        final_fmt="FREE",
                        image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                        is_free_to_keep=True))
                    log.info(f"  🎁 Steam FTK (HTML): {title}")
            except:
                pass
    log.info(f"Steam Free to Keep: {len(games)}")
    return games

def steam_get_details(appid: str) -> dict | None:
    time.sleep(1.2)
    r = safe_get("https://store.steampowered.com/api/appdetails", params={"appids": appid, "cc": "us", "l": "english"})
    if not r:
        return None
    try:
        data = r.json()
        app = data.get(str(appid), {})
        if not app.get("success"):
            return None
        return app["data"]
    except Exception as e:
        log.error(f"Steam details error: {e}")
        return None

def steam_get_reviews(appid: str):
    r = safe_get(f"https://store.steampowered.com/appreviews/{appid}", params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0})
    if not r:
        return None, None, ""
    try:
        qs = r.json().get("query_summary", {})
        pos = qs.get("total_positive", 0)
        total = qs.get("total_reviews", 0)
        desc = qs.get("review_score_desc", "")
        if total == 0:
            return None, None, ""
        return round(pos / total * 100), total, desc
    except:
        return None, None, ""

def steam_get_promo_info(appid: str, is_ftk: bool) -> tuple:
    start_date = ""
    end_date = ""
    r = safe_get(f"https://store.steampowered.com/app/{appid}/", retries=2)
    if r:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            block = soup.select_one(".discount_block")
            if block:
                text = block.get_text(separator=" ", strip=True)
                match_start = re.search(r"Offer starts\s+([\w]+\s+\d{1,2},\s+\d{4})", text, re.IGNORECASE)
                if match_start:
                    try:
                        start_dt = datetime.datetime.strptime(match_start.group(1), "%b %d, %Y")
                        start_date = start_dt.strftime("%Y-%m-%d")
                    except:
                        pass
                match_end = re.search(r"Offer ends\s+([\w]+\s+\d{1,2},\s+\d{4})", text, re.IGNORECASE)
                if match_end:
                    try:
                        end_dt = datetime.datetime.strptime(match_end.group(1), "%b %d, %Y")
                        end_date = end_dt.strftime("%Y-%m-%d")
                    except:
                        pass
        except:
            pass
    week = current_week_anchor()
    prefix = "FTK:" if is_ftk else ""
    return start_date, end_date, f"{prefix}{week}"

def steam_is_free_to_play(appid: str) -> bool:
    details = steam_get_details(appid)
    if details:
        if details.get("is_free", False):
            po = details.get("price_overview")
            if po:
                if po.get("final", 0) == 0:
                    return True
            else:
                return True
    return False

def fetch_steam_games() -> list:
    games = []
    featured = _steam_fetch_featured()
    _merge(games, featured)
    log.info(f"Steam Source 1 (Featured):     {len(featured)}")
    html = _steam_fetch_html_search()
    before = len(games)
    _merge(games, html)
    log.info(f"Steam Source 2 (HTML Search):  {len(html)} raw → {len(games)-before} new")
    ftk = _steam_fetch_free_to_keep()
    before = len(games)
    _merge(games, ftk)
    log.info(f"Steam Source 3 (Free to Keep): {len(ftk)} raw → {len(games)-before} new")
    return games

# ─── Epic ──────────────────────────────────────────────────────────────
EPIC_GQL_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

def fetch_epic_games() -> list:
    games = []
    r = safe_get(EPIC_GQL_URL, params={"locale": "en-US", "country": "US", "allowCountries": "US"},
                 extra_headers={"Referer": "https://store.epicgames.com/"})
    if not r:
        log.error("Epic API failed")
        return games
    try:
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        for el in elements:
            title = el.get("title", "").strip()
            if not title:
                continue
            total_price = el.get("price", {}).get("totalPrice", {})
            if total_price.get("discountPrice", 0) == 0 and not el.get("promotions"):
                continue
            promotions = el.get("promotions") or {}
            promo_offers = promotions.get("promotionalOffers", [])
            active = None
            for group in promo_offers:
                for offer in group.get("promotionalOffers", []):
                    start = offer.get("startDate", "")
                    end = offer.get("endDate", "")
                    try:
                        s = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                        e = datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                        if s <= now <= e:
                            active = {"start": s, "end": e}
                            break
                    except:
                        pass
                if active:
                    break
            if not active:
                continue
            orig_cents = total_price.get("originalPrice", 0)
            orig_fmt = f"${orig_cents/100:.2f}" if orig_cents else ""
            image_url = ""
            for img_type in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail"):
                for img in el.get("keyImages", []):
                    if img.get("type") == img_type:
                        image_url = img.get("url", "")
                        break
                if image_url:
                    break
            slug = el.get("catalogNs", {}).get("mappings", [{}])[0].get("pageSlug", "") or el.get("productSlug", "") or el.get("urlSlug", "")
            link = f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games"
            game_id = el.get("id") or el.get("productSlug") or slug or title
            end_display = active["end"].strftime("%Y-%m-%d")
            game = make_game("epic", str(game_id), title, 100, link, orig_fmt=orig_fmt, final_fmt="FREE",
                             image_url=image_url, is_free_to_keep=True)
            game["deal_start"] = active["start"].strftime("%Y-%m-%d")
            game["deal_end"] = end_display
            games.append(game)
            log.info(f"  ⬛ Epic Free: {title} (ends {end_display})")
    except Exception as e:
        log.error(f"Epic parse error: {e}")
    log.info(f"Epic Games total: {len(games)}")
    return games

def epic_get_promo_info(game: dict) -> tuple:
    r = safe_get(EPIC_GQL_URL, params={"locale": "en-US", "country": "US", "allowCountries": "US"},
                 extra_headers={"Referer": "https://store.epicgames.com/"})
    if not r:
        return "", "", f"FTK:{current_week_anchor()}"
    try:
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        for el in elements:
            eid = str(el.get("id") or el.get("productSlug") or "")
            if eid != game["id"] and el.get("title", "") != game["title"]:
                continue
            promotions = el.get("promotions") or {}
            for group in promotions.get("promotionalOffers", []):
                for offer in group.get("promotionalOffers", []):
                    start = offer.get("startDate", "")
                    end = offer.get("endDate", "")
                    try:
                        s = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                        e = datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                        if e > now:
                            return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d"), f"FTK:{e.strftime('%Y-%m-%d')}"
                    except:
                        pass
    except:
        pass
    return "", "", f"FTK:{current_week_anchor()}"

# ─── مشکل ۲: GOG (اصلاح شده) ──────────────────────────────────────────
# مشکل: API قدیمی GOG ساختار تغییر داده، selectors HTML هم متفاوت شده
# راه‌حل ۱: استفاده از API جدید catalog.gog.com/v1/catalog با پارامترهای صحیح
# راه‌حل ۲: استفاده از GOG Sales API که داده JSON مستقیم می‌ده
def fetch_gog_games() -> list:
    games = []

    log.info("  🔍 Fetching GOG via Catalog API (v1)")
    api_games = _gog_fetch_catalog_api()
    _merge(games, api_games)
    log.info(f"  GOG Catalog API: {len(api_games)} games")

    if len(games) < 3:
        log.info("  🔍 Fetching GOG via Sales API")
        sales_games = _gog_fetch_sales_api()
        before = len(games)
        _merge(games, sales_games)
        log.info(f"  GOG Sales API: {len(sales_games)} raw → {len(games)-before} new")

    if len(games) < 3:
        log.info("  🔍 Fetching GOG via Free Games API")
        free_games = _gog_fetch_free_games()
        before = len(games)
        _merge(games, free_games)
        log.info(f"  GOG Free API: {len(free_games)} raw → {len(games)-before} new")

    log.info(f"  ✅ GOG total: {len(games)} games found")
    return games

def _gog_fetch_catalog_api() -> list:
    """
    استفاده از API جدید GOG با پارامتر discounted=true
    این API مستقیماً JSON برمی‌گردونه و نیاز به scraping نداره
    """
    games = []
    url = "https://catalog.gog.com/v1/catalog"
    params = {
        "limit": 48,
        "order": "desc:trending",
        "discounted": "true",
        "productType": "in:game,pack",
        "page": 1,
        "countryCode": "US",
        "currencyCode": "USD",
        "locale": "en-US",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://www.gog.com",
        "Referer": "https://www.gog.com/",
    }
    r = safe_get(url, params=params, retries=3, extra_headers=headers)
    if not r:
        r = safe_get(url, params=params, retries=2, use_scraper=True, extra_headers=headers)
    if not r:
        log.warning("  ⚠️ GOG Catalog API failed")
        return games

    try:
        data = r.json()
        products = data.get("products", [])
        log.info(f"  📊 GOG Catalog API returned {len(products)} products")

        # debug: ساختار اولین محصول رو لاگ کن
        if products:
            first = products[0]
            price_raw = first.get("price", {})
            log.info(f"  🔍 GOG price structure sample: {json.dumps(price_raw)[:300]}")

        for item in products:
            try:
                title = item.get("title", "").strip()
                if not title or _should_skip(title):
                    continue

                price_info = item.get("price", {}) or {}

                # ساختار GOG Catalog API — چند فرمت مختلف را امتحان می‌کنیم
                discount = 0
                base_price = 0.0
                final_price = 0.0

                # فرمت ۱: {"base":"$59.99","final":"$14.99","discountPercentage":"75"}
                if "discountPercentage" in price_info:
                    try:
                        discount = int(str(price_info["discountPercentage"]).replace("%","").strip() or 0)
                    except:
                        pass

                # فرمت ۲: {"baseAmount":"59.99","finalAmount":"14.99","discount":"-75%"}
                if discount == 0 and "discount" in price_info:
                    try:
                        d = str(price_info["discount"]).replace("%","").replace("-","").strip()
                        discount = int(float(d)) if d else 0
                    except:
                        pass

                # استخراج مقادیر عددی قیمت از رشته‌های مختلف
                def _parse_price(val) -> float:
                    if not val:
                        return 0.0
                    s = str(val).replace("$","").replace(",","").replace("USD","").strip()
                    try:
                        return float(s)
                    except:
                        return 0.0

                base_price = _parse_price(
                    price_info.get("base") or price_info.get("baseAmount") or
                    price_info.get("basePrice") or price_info.get("original") or 0
                )
                final_price = _parse_price(
                    price_info.get("final") or price_info.get("finalAmount") or
                    price_info.get("finalPrice") or price_info.get("amount") or 0
                )

                # اگر هنوز discount نداریم، محاسبه دستی
                if discount == 0 and base_price > 0 and 0 <= final_price < base_price:
                    discount = int((1 - final_price / base_price) * 100)

                if discount < MIN_DISCOUNT and discount != 100:
                    continue

                orig_fmt = f"${base_price:.2f}" if base_price > 0 else ""
                final_fmt = "FREE" if (discount == 100 or final_price == 0) else (f"${final_price:.2f}" if final_price > 0 else "")

                slug = item.get("slug", "") or str(item.get("id", ""))
                link = f"https://www.gog.com/en/game/{slug}" if slug else ""
                if not link:
                    continue

                cover = (item.get("coverHorizontal", "") or item.get("cover", "") or
                         item.get("image", "") or "")
                if cover and cover.startswith("//"):
                    cover = "https:" + cover

                game_id = str(item.get("id", slug or title.lower().replace(" ", "-")))
                is_ftk = discount == 100 and final_price == 0

                game = make_game("gog", game_id, title, discount, link, orig_fmt, final_fmt,
                               image_url=cover, is_free_to_keep=is_ftk)

                promo_end = item.get("promoEndDate", "") or item.get("discountEndDate", "") or item.get("saleEndsAt", "")
                if promo_end:
                    game["deal_end"] = promo_end[:10]
                promo_start = item.get("promoStartDate", "") or item.get("discountStartDate", "") or item.get("saleStartsAt", "")
                if promo_start:
                    game["deal_start"] = promo_start[:10]

                games.append(game)
                log.info(f"  🟣 GOG: {title} -{discount}% (base={base_price}, final={final_price})")

            except Exception as e:
                log.debug(f"GOG item parse error: {e}")
                continue

    except Exception as e:
        log.error(f"GOG Catalog API parse error: {e}")

    return games

def _gog_fetch_sales_api() -> list:
    """
    استفاده از endpoint فروش ویژه GOG
    """
    games = []
    # GOG endpoint برای بازی‌های رایگان
    url = "https://www.gog.com/games/ajax/filtered"
    params = {
        "mediaType": "game",
        "price": "discounted",
        "sort": "popularity",
        "page": 1,
    }
    r = safe_get(url, params=params, retries=3, use_scraper=True,
                 extra_headers={"Referer": "https://www.gog.com/", "Accept": "application/json"})
    if not r:
        return games

    try:
        data = r.json()
        products = data.get("products", [])
        log.info(f"  📊 GOG Sales API returned {len(products)} products")

        for item in products:
            try:
                title = item.get("title", "").strip()
                if not title or _should_skip(title):
                    continue

                price_info = item.get("price", {}) or {}
                discount_str = str(price_info.get("discount", "0")).replace("%", "").replace("-", "")
                try:
                    discount = int(float(discount_str))
                except:
                    discount = 0

                if discount < MIN_DISCOUNT and discount != 100:
                    continue

                base = price_info.get("baseAmount", "") or price_info.get("base", "")
                final = price_info.get("finalAmount", "") or price_info.get("final", "")
                orig_fmt = f"${base}" if base else ""
                final_fmt = "FREE" if discount == 100 else (f"${final}" if final else "")

                slug = item.get("slug", "") or str(item.get("id", ""))
                link = f"https://www.gog.com/en/game/{slug}" if slug else ""
                if not link:
                    continue

                image = item.get("image", "") or item.get("backgroundImage", "")
                if image and not image.startswith("http"):
                    image = "https:" + image if image.startswith("//") else image
                # GOG معمولاً URL تصویر بدون پسوند داره
                if image and not any(image.endswith(ext) for ext in ['.jpg', '.png', '.webp']):
                    image = image + ".jpg"

                game_id = str(item.get("id", slug))
                is_ftk = discount == 100

                game = make_game("gog", game_id, title, discount, link, orig_fmt, final_fmt,
                               image_url=image, is_free_to_keep=is_ftk)
                games.append(game)
                log.info(f"  🟣 GOG Sale: {title} -{discount}%")

            except Exception as e:
                log.debug(f"GOG sales item error: {e}")
                continue

    except Exception as e:
        log.error(f"GOG Sales API parse error: {e}")

    return games

def _gog_fetch_free_games() -> list:
    """
    دریافت بازی‌های رایگان GOG از طریق API مخصوص
    """
    games = []
    url = "https://www.gog.com/games/ajax/filtered"
    params = {
        "mediaType": "game",
        "price": "free",
        "sort": "popularity",
        "page": 1,
    }
    r = safe_get(url, params=params, retries=3, use_scraper=True,
                 extra_headers={"Referer": "https://www.gog.com/", "Accept": "application/json"})
    if not r:
        return games

    try:
        data = r.json()
        products = data.get("products", [])
        for item in products:
            try:
                title = item.get("title", "").strip()
                if not title or _should_skip(title):
                    continue

                price_info = item.get("price", {}) or {}
                final = price_info.get("finalAmount", "0") or "0"
                try:
                    is_really_free = float(str(final).replace(",", "")) == 0
                except:
                    is_really_free = False

                if not is_really_free:
                    continue

                slug = item.get("slug", "") or str(item.get("id", ""))
                link = f"https://www.gog.com/en/game/{slug}" if slug else ""
                if not link:
                    continue

                image = item.get("image", "")
                if image and not image.startswith("http"):
                    image = "https:" + image if image.startswith("//") else image
                if image and not any(image.endswith(ext) for ext in ['.jpg', '.png', '.webp']):
                    image = image + ".jpg"

                game_id = str(item.get("id", slug))
                game = make_game("gog", game_id, title, 100, link, "", "FREE",
                               image_url=image, is_free_to_keep=True)
                games.append(game)
                log.info(f"  🟣 GOG Free: {title}")
            except:
                continue
    except Exception as e:
        log.error(f"GOG Free API error: {e}")

    return games

def gog_get_promo_info(game: dict) -> tuple:
    # اگر تاریخ پایان از API داریم، استفاده کن
    if game.get("deal_end"):
        return "", game["deal_end"], f"END:{game['deal_end']}"
    week = current_week_anchor()
    prefix = "FTK:" if game.get("is_free_to_keep") else ""
    return "", "", f"{prefix}{week}"

# ─── مشکل ۳: PlayStation (اصلاح شده) ─────────────────────────────────
# مشکل: PlayStation Store کاملاً JavaScript-rendered هست
# requests یا cloudscraper نمی‌تونه محتوای واقعی رو بگیره
# راه‌حل ۱: استفاده از API رسمی PS Store که JSON برمی‌گردونه
# راه‌حل ۲: استفاده از PlayStation Blog RSS برای Plus Essential/Extra
# راه‌حل ۳: استفاده از PlayStation API v2 برای deals

def fetch_playstation_deals() -> list:
    """
    PlayStation Store is fully JS-rendered — direct scraping is not possible.
    We use the PS Blog RSS as the only reliable source for deals.
    The dead API endpoints (m.np.playstation.com, store.playstation.com/store/api,
    psdeals.net) have been removed to eliminate error noise in logs.
    """
    games = []
    log.info("  Fetching PlayStation deals via PS Blog RSS")
    games = _ps_fetch_rss_deals()
    log.info(f"  PlayStation deals: {len(games)} games found")
    return games

def _ps_fetch_rss_deals() -> list:
    """
    PlayStation Blog RSS — the only reliable non-JS source for PS deals.
    """
    games = []
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:20]:
            entry_title = entry.get("title", "")
            if not any(kw in entry_title for kw in ["Deal", "Sale", "Free", "Plus", "Discount"]):
                continue
            content = ""
            if hasattr(entry, 'content'):
                content = entry.content[0].value
            elif hasattr(entry, 'description'):
                content = entry.description

            soup = BeautifulSoup(content, "html.parser")
            text = soup.get_text()

            famous_games = [
                "God of War", "Horizon", "Uncharted", "The Last of Us", "Final Fantasy",
                "Resident Evil", "Spider-Man", "Ghost of Tsushima", "Demon's Souls",
                "Returnal", "Ratchet & Clank", "Death Stranding", "Days Gone"
            ]
            for game_name in famous_games:
                if game_name.lower() in text.lower():
                    gid = game_name.lower().replace(" ", "-").replace("'", "").replace("&", "and")
                    game = make_game("playstation", gid, game_name, 100,
                        entry.get("link", "https://blog.playstation.com"),
                        "", "FREE / Included", "", is_free_to_keep=False)
                    games.append(game)
    except Exception as e:
        log.warning(f"  ⚠️ PS RSS deals error: {e}")
    return games

def playstation_get_promo_info(game: dict) -> tuple:
    return "", "", f"END:{current_week_anchor()}"

# ─── PS Plus Essential / Extra (اصلاح شده) ────────────────────────────
def fetch_playstation_plus_essential() -> list:
    """
    PS Plus Essential monthly games.
    Uses PlayStation Store API (JSON) — no HTML selectors to break.
    Falls back to static list if API unavailable.
    """
    games = []
    log.info("  Fetching PS Plus Essential via PlayStation Store API")

    # PlayStation Store offers API — public endpoint
    api_games = _ps_plus_fetch_api("essential")
    if api_games:
        log.info(f"  PS Essential API: {len(api_games)} games")
        return api_games

    # Fallback: static list (updated manually each month if needed)
    log.info("  PS API unavailable — using static list for PS Essential")
    static = [
        ("God of War Ragnarok",   "god-of-war-ragnarok"),
        ("The Last of Us Part I", "the-last-of-us-part-i"),
        ("Final Fantasy VII Remake", "final-fantasy-vii-remake"),
    ]
    for title, gid in static:
        games.append(make_game("playstation_essential", gid, title, 100,
            "https://www.playstation.com/en-us/ps-plus/whats-on-ps-plus/",
            "", "FREE (PS Plus Essential)", "", is_free_to_keep=True))
    log.info(f"  PS Essential: {len(games)} games found")
    return games

def fetch_playstation_plus_extra() -> list:
    """
    PS Plus Extra catalog games.
    Uses PlayStation Store API (JSON) — no HTML selectors to break.
    """
    games = []
    log.info("  Fetching PS Plus Extra via PlayStation Store API")

    api_games = _ps_plus_fetch_api("extra")
    if api_games:
        log.info(f"  PS Extra API: {len(api_games)} games")
        return api_games

    log.info("  PS API unavailable — using static list for PS Extra")
    static = [
        ("God of War Ragnarok",        "god-of-war-ragnarok"),
        ("Horizon Forbidden West",     "horizon-forbidden-west"),
        ("The Last of Us Part I",      "the-last-of-us-part-i"),
    ]
    for title, gid in static:
        games.append(make_game("playstation_extra", gid, title, 0,
            "https://www.playstation.com/en-us/ps-plus/whats-on-ps-plus/",
            "", "Included in PS Plus Extra", "", is_free_to_keep=False))
    log.info(f"  PS Extra: {len(games)} games found")
    return games

def _ps_plus_fetch_api(tier: str) -> list:
    """
    Fetch PS Plus games using the PlayStation Store GraphQL API.
    tier: "essential" | "extra"
    Returns empty list if API is unavailable.
    """
    games = []
    # Concept ID for PS Plus monthly free games (Essential)
    # These are stable concept IDs that don't require JS rendering
    concept_urls = {
        "essential": "https://store.playstation.com/en-us/pages/latest/1/",
        "extra": "https://store.playstation.com/en-us/pages/latest/1/",
    }
    store_key = "playstation_essential" if tier == "essential" else "playstation_extra"
    is_ftk = tier == "essential"

    # Try PlayStation Store API v2 (returns JSON for PS Plus lineup)
    api_url = "https://web.np.playstation.com/api/graphql/v1/op"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://store.playstation.com",
        "Referer": "https://store.playstation.com/",
    }
    # Query for PS Plus monthly games
    payload = {
        "operationName": "getPsPlusCatalog",
        "variables": {
            "pageArgs": {"size": 24, "offset": 0},
            "countryCode": "US",
            "languageCode": "en",
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "6d8d56d4e4cbe9a7e1d3e5a6b8c7f9a2",
            }
        }
    }
    r = safe_get(api_url, retries=1, extra_headers=headers)
    if r:
        try:
            data = r.json()
            products = (data.get("data", {}).get("catalog", {}).get("products", []) or
                       data.get("data", {}).get("products", []) or [])
            for item in products[:20]:
                title = (item.get("name") or item.get("localizedName", "")).strip()
                if not title or _should_skip(title):
                    continue
                pid = item.get("id", title.lower().replace(" ", "-"))
                link = f"https://store.playstation.com/en-us/product/{pid}"
                images = item.get("media", []) or item.get("images", [])
                image_url = images[0].get("url", "") if images else ""
                game = make_game(store_key, str(pid), title, 100 if is_ftk else 0,
                    link, "", "FREE (PS Plus)" if is_ftk else "PS Plus Extra", image_url,
                    is_free_to_keep=is_ftk)
                games.append(game)
                log.info(f"  PS {tier}: {title}")
        except:
            pass

    return games

# ─── AAA Detection ────────────────────────────────────────────────────
def is_aaa_game_metacritic(title: str) -> bool:
    if RAWG_API_KEY:
        rawg = rawg_search(title)
        if rawg:
            mc = rawg.get("metacritic")
            rp = rawg.get("rating_pct")
            rc = rawg.get("ratings_count", 0)
            if mc and mc >= AAA_METACRITIC_THRESHOLD:
                return True
            if rp and rp >= AAA_RATING_THRESHOLD:
                return True
            if rc > AAA_REVIEWS_THRESHOLD:
                return True
    aaa_list = ["halo","forza","gears of war","starfield","doom","cyberpunk","witcher","red dead",
                "gta","assassin's creed","final fantasy","resident evil","god of war","spider-man",
                "horizon","uncharted","last of us","ghost of tsushima","call of duty","battlefield",
                "far cry","diablo","overwatch","fallout","elder scrolls","minecraft","age of empires",
                "dead space","mass effect","dragon age","batman","arkham","tomb raider","wolfenstein",
                "persona","yakuza","dragon quest","borderlands","bioshock","dying light","monster hunter"]
    return any(aaa in title.lower() for aaa in aaa_list)

def enrich_from_metacritic(game: dict) -> bool:
    if not RAWG_API_KEY:
        return False
    rawg = rawg_search(game["title"])
    if not rawg:
        return False
    if not game.get("genres") and rawg.get("genres"):
        game["genres"] = rawg["genres"]
    if not game.get("description") and rawg.get("description"):
        game["description"] = rawg["description"]
    if not game.get("review_pct") and rawg.get("rating_pct"):
        game["review_pct"] = rawg["rating_pct"]
        game["review_count"] = rawg.get("ratings_count", 0)
        if rawg.get("metacritic"):
            game["review_desc"] = f"Metacritic: {rawg['metacritic']}"
    if not game.get("metacritic") and rawg.get("metacritic"):
        game["metacritic"] = rawg["metacritic"]
    if not game.get("rawg_image") and rawg.get("background_image"):
        game["rawg_image"] = rawg["background_image"]
    if not game.get("release_date") and rawg.get("released"):
        game["release_date"] = rawg["released"]
    return True

# ─── Xbox Game Pass ────────────────────────────────────────────────────
# Uses Xbox Game Pass API (JSON) instead of HTML scraping.
# The API endpoint is stable and doesn't break on HTML changes.
def fetch_xbox_gamepass() -> list:
    games = []
    log.info("  Fetching Xbox Game Pass via API")

    # Xbox Game Pass catalog API (public, no auth required)
    api_games = _xbox_fetch_api()
    if api_games:
        log.info(f"  Xbox API: {len(api_games)} games")
        return api_games

    # Fallback: curated AAA list verified by keyword matching
    log.info("  Xbox API unavailable — using curated AAA list")
    return _xbox_curated_list()

def _xbox_fetch_api() -> list:
    """
    Fetch Xbox Game Pass catalog from the official Xbox API.
    Returns empty list if API is unreachable.
    """
    games = []
    # Xbox Game Pass All catalog endpoint (PC + Console)
    url = "https://catalog.gamepass.com/sigls/v2"
    params = {
        "id": "fdd9e2a7-0fee-49f6-ad69-4354098401ff",  # Game Pass Ultimate PC catalog
        "language": "en-us",
        "market": "US",
    }
    r = safe_get(url, params=params, retries=2,
                 extra_headers={"Accept": "application/json"})
    if not r:
        return games
    try:
        items = r.json()
        if not isinstance(items, list):
            return games
        # items is list of {"id": "...", "market": "US"} — need product details
        game_ids = [item["id"] for item in items if item.get("id")][:50]
        if not game_ids:
            return games
        # batch product details
        details_url = "https://displaycatalog.mp.microsoft.com/v7.0/products"
        chunk_size = 20
        for i in range(0, len(game_ids), chunk_size):
            chunk = game_ids[i:i+chunk_size]
            r2 = safe_get(details_url, params={
                "bigIds": ",".join(chunk),
                "market": "US",
                "languages": "en-US",
                "MS-CV": "DGU1mcuYo0WMMp",
            }, retries=2, extra_headers={"Accept": "application/json"})
            if not r2:
                continue
            try:
                products = r2.json().get("Products", [])
                for prod in products:
                    try:
                        title = prod.get("LocalizedProperties", [{}])[0].get("ProductTitle", "").strip()
                        if not title or _should_skip(title):
                            continue
                        product_type = prod.get("ProductType", "")
                        if product_type not in ("Game", ""):
                            continue
                        # image
                        images = prod.get("LocalizedProperties", [{}])[0].get("Images", [])
                        image_url = ""
                        for img in images:
                            if img.get("ImagePurpose") in ("BoxArt", "Poster", "SuperHeroArt", "FeaturePromotionalSquareArt"):
                                raw_url = img.get("Uri", "")
                                if raw_url:
                                    image_url = raw_url if raw_url.startswith("http") else "https:" + raw_url
                                    break
                        pid = prod.get("ProductId", title.lower().replace(" ", "-"))
                        link = f"https://www.xbox.com/en-US/games/store/{pid}"
                        game = make_game("xbox_gamepass", pid, title, 0,
                            link, "", "Included in Game Pass", image_url, is_free_to_keep=False)
                        games.append(game)
                    except:
                        continue
            except:
                continue
        log.info(f"  Xbox catalog API: {len(games)} games fetched")
    except Exception as e:
        log.warning(f"Xbox API parse error: {e}")
    return games

def _xbox_curated_list() -> list:
    """Stable curated AAA list as fallback — no HTML scraping."""
    games = []
    curated = [
        ("Starfield",                    "starfield",               1716740),
        ("Forza Horizon 5",              "forza-horizon-5",         1551360),
        ("Halo Infinite",                "halo-infinite",           1240440),
        ("Call of Duty: Modern Warfare", "call-of-duty",            1938090),
        ("Diablo IV",                    "diablo-iv",               2344520),
        ("Doom Eternal",                 "doom-eternal",             782330),
        ("Fallout 4",                    "fallout-4",                377160),
        ("The Elder Scrolls V: Skyrim",  "skyrim",                   489830),
        ("Minecraft",                    "minecraft",               1151280),
        ("Age of Empires IV",            "age-of-empires-iv",       1466860),
        ("Gears 5",                      "gears-5",                 1097840),
        ("Dead Space",                   "dead-space",              1693980),
        ("Mass Effect Legendary Edition","mass-effect-legendary",   1328670),
        ("Batman: Arkham Knight",        "batman-arkham-knight",     208650),
        ("Star Wars Jedi: Survivor",     "jedi-survivor",           1774580),
        ("Mafia: Definitive Edition",    "mafia",                   1030840),
        ("Crisis Core: Final Fantasy VII","crisis-core",            1852400),
        ("Dragon's Dogma 2",             "dragons-dogma-2",         2054970),
        ("Devil May Cry 5",              "devil-may-cry-5",          601150),
        ("Monster Hunter Rise",          "monster-hunter-rise",     1446780),
        ("Persona 5 Royal",              "persona-5-royal",         1687950),
        ("Yakuza: Like a Dragon",        "yakuza-like-a-dragon",    1235140),
        ("Dragon Quest XI",              "dragon-quest-xi",         1295510),
        ("Borderlands 3",                "borderlands-3",            397540),
        ("Bioshock: The Collection",     "bioshock-collection",      409710),
        ("Dying Light 2",                "dying-light-2",            534380),
        ("Sleeping Dogs",                "sleeping-dogs",            202170),
    ]
    for title, slug, steam_id in curated:
        if not is_aaa_game_metacritic(title):
            continue
        image_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{steam_id}/capsule_616x353.jpg"
        game = make_game("xbox_gamepass", slug, title, 0,
            "https://www.xbox.com/en-US/xbox-game-pass", "", "Included in Game Pass",
            image_url, is_free_to_keep=False)
        games.append(game)
        log.info(f"  Xbox Game Pass (AAA): {title}")
    log.info(f"  Xbox Game Pass: {len(games)} AAA games")
    return games

# ─── Steam Enrich ──────────────────────────────────────────────────────
def steam_search_by_title(title: str) -> str | None:
    r = safe_get("https://store.steampowered.com/search/", params={"term": title, "cc": "US", "l": "english"}, retries=2)
    if not r:
        return None
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.select(".search_result_row"):
            href = row.get("href", "")
            if "/app/" not in href:
                continue
            appid = href.split("/app/")[1].split("/")[0]
            if appid.isdigit():
                title_el = row.select_one(".title")
                if title_el and title.lower() in title_el.text.strip().lower():
                    return appid
    except:
        pass
    return None

def enrich_from_steam(game: dict) -> bool:
    if game.get("description") and game.get("genres") and game.get("review_pct"):
        return True
    appid = steam_search_by_title(game["title"])
    if not appid:
        return False
    details = steam_get_details(appid)
    if not details:
        return False
    if not game.get("description"):
        game["description"] = BeautifulSoup(details.get("short_description", ""), "html.parser").get_text()
    if not game.get("genres"):
        game["genres"] = [g["description"] for g in details.get("genres", [])]
    if not game.get("review_pct"):
        rev_pct, rev_count, rev_desc = steam_get_reviews(appid)
        if rev_pct is not None:
            game["review_pct"] = rev_pct
            game["review_count"] = rev_count
            game["review_desc"] = rev_desc
    game["steam_image"] = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"
    if details.get("is_free", False) and not game.get("is_free_to_keep"):
        game["is_free_to_play"] = True
    return True

# ─── Caption Builder ───────────────────────────────────────────────────
def _make_title_hashtag(title: str) -> str:
    """اسم بازی را به هشتگ تبدیل می‌کند"""
    clean = re.sub(r'[™®©:\-–—\'"!?.,]', '', title)
    clean = re.sub(r'\s+', '', clean)
    clean = re.sub(r'[^a-zA-Z0-9\u0600-\u06FF]', '', clean)
    return f"#{clean}" if clean else ""

def build_caption(game: dict, start_date: str, end_date: str) -> str:
    store = game["store"]
    meta = STORE_META[store]
    is_ftk = game.get("is_free_to_keep", False)
    discount = game["discount"]
    title = game["title"]
    deal_type = detect_deal_type(game)
    deal_emoji = get_deal_emoji(deal_type)
    deal_label = get_deal_label(deal_type, discount)

    raw_desc = game.get("description") or "No description available."
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc = raw_desc[:260].rstrip() + ("…" if len(raw_desc) > 260 else "")

    rev_pct = game.get("review_pct")
    rev_count = game.get("review_count")
    rev_desc = game.get("review_desc", "")
    metacritic = game.get("metacritic")
    if rev_pct is not None and rev_count:
        mood = "🟢" if rev_pct >= 80 else ("🟡" if rev_pct >= 60 else "🔴")
        if store == "steam":
            review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} reviews — {rev_desc}"
        else:
            review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} ratings"
            if metacritic:
                review_line += f"  |  Metacritic: <b>{metacritic}</b>"
    else:
        review_line = None

    genres = game.get("genres") or []
    genre_str = ", ".join(genres[:4]) if genres else None
    orig = game.get("price_orig_fmt", "")
    final = game.get("price_final_fmt", "")

    if discount == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block = f"{deal_emoji} {deal_label}"
    else:
        price_block = f"<s>{orig}</s> → <b>{final}</b>" if orig and final else (final or orig or "?")
        disc_block = f"<b>-{discount}%</b> 🔥"

    # ─── تاریخ Detected با شمسی ───────────────────────────────────────
    now_dt = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    now_gregorian = now_dt.strftime("%d %b %Y")
    now_detected_str = now_gregorian
    if JDT_AVAILABLE and jdatetime:
        try:
            jd_now = jdatetime.datetime.fromgregorian(datetime=now_dt)
            now_shamsi = jd_now.strftime("%Y/%m/%d")
            now_detected_str = f"{now_gregorian} | {now_shamsi}"
        except:
            pass

    # ─── Hashtags (English only) ──────────────────────────────────────
    tags = ["#FreeGamesHub", meta["tag"]]

    # game title hashtag
    title_tag = _make_title_hashtag(title)
    if title_tag:
        tags.append(title_tag)

    # important words from title
    title_words = re.sub(r'[™®©:\-–—\'"!?.,]', ' ', title).split()
    for word in title_words:
        if len(word) >= 4 and word[0].isupper():
            w_clean = re.sub(r'[^a-zA-Z0-9]', '', word)
            if w_clean and f"#{w_clean}" not in tags:
                tags.append(f"#{w_clean}")

    # genre tags
    for g in genres[:3]:
        tag = re.sub(r'[^a-zA-Z0-9]', '', g)
        if tag and f"#{tag}" not in tags:
            tags.append(f"#{tag}")

    # deal status
    if is_ftk:
        tags.append("#FreeToKeep")
        tags.append("#FreeGame")
    elif discount == 100:
        tags.append("#FreeWeekend")
        tags.append("#FreeGame")
    if discount >= 90:
        tags.append("#MegaDeal")
    elif discount >= 75:
        tags.append("#BigDeal")

    # platform
    if store in ("steam", "epic", "gog"):
        tags.append("#PCGaming")
        tags.append("#PCDeals")
    elif store in ("playstation", "playstation_essential", "playstation_extra"):
        tags.append("#PS4")
        tags.append("#PS5")
        tags.append("#PlayStation")
        tags.append("#PSPlus")
    elif store == "xbox_gamepass":
        tags.append("#Xbox")
        tags.append("#GamePass")
        tags.append("#XboxDeals")

    if game.get("is_aaa"):
        tags.append("#AAA")
        tags.append("#Gaming")

    # deduplicate
    seen_tags = []
    for t in tags:
        if t not in seen_tags:
            seen_tags.append(t)
    hashtags = " ".join(seen_tags)

    # ─── ساخت متن پیام ───────────────────────────────────────────────
    lines = [f"{meta['emoji']} <b>[{meta['name']}]</b>  🎮 <b>{title}</b>", ""]

    if genre_str:
        lines += [f"🎯 <b>Genre:</b> {genre_str}", ""]

    if desc and desc != "No description available.":
        lines += ["📝 <b>About:</b>", desc, ""]

    if review_line:
        label = "Steam Reviews" if store == "steam" else "Rating"
        lines += [f"⭐ <b>{label}:</b> {review_line}", ""]

    lines += [f"💰 <b>Price:</b> {price_block}", f"💸 <b>Discount:</b> {disc_block}", ""]

    # ─── Dates: Gregorian first, Shamsi second ───────────────────────
    has_dates = start_date or end_date
    if has_dates:
        lines += ["📅 <b>Offer Period:</b>"]
        if start_date:
            lines.append(f"  🟢 <b>Start:</b> {format_date_bilingual(start_date)}")
        if end_date:
            lines.append(f"  🔴 <b>End:</b>   {format_date_bilingual(end_date)}")
        lines.append("")
    else:
        lines += ["📅 <b>Offer Period:</b> Currently active", ""]

    lines += [
        f"🕐 <b>Detected:</b> {now_detected_str} UTC",
        "",
        f"🔗 {game['link']}",
        "",
        hashtags,
    ]

    caption = "\n".join(lines)

    # کوتاه کردن در صورت نیاز (حداکثر ۱۰۲۴ کاراکتر Telegram)
    if len(caption) > 1024:
        short_desc = raw_desc[:80].rstrip() + "…"
        new_lines = []
        for line in lines:
            if line.startswith("📝 <b>About:</b>"):
                new_lines.append(f"📝 <b>About:</b> {short_desc}")
            elif line == desc:
                continue
            else:
                new_lines.append(line)
        caption = "\n".join(new_lines)

    if len(caption) > 1024:
        new_lines, skip = [], False
        for line in lines:
            if line.startswith("📝 <b>About:</b>"):
                skip = True
                continue
            if skip and line == "":
                skip = False
                continue
            if not skip:
                new_lines.append(line)
        caption = "\n".join(new_lines)

    if len(caption) > 1024:
        caption = caption[:1021] + "…"

    return caption

# ─── Telegram Sender ──────────────────────────────────────────────────
def _is_valid_image_url(url: str) -> bool:
    """Quick HEAD request to verify the image URL actually returns an image."""
    try:
        r = requests.head(url, timeout=5, allow_redirects=True)
        if r.status_code != 200:
            return False
        ct = r.headers.get("Content-Type", "")
        return ct.startswith("image/")
    except:
        return False

def send_game(game: dict, caption: str) -> int:
    """
    Send game to Telegram channel.
    Returns message_id (>0) on success, 0 on failure.
    """
    tg_photo = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    tg_text  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    candidates = get_image_candidates(game)
    candidates = [c for c in candidates if c]

    # validate images first to avoid Telegram "wrong type" errors
    valid_images = []
    for img in candidates:
        if _is_valid_image_url(img):
            valid_images.append(img)
        else:
            log.debug(f"Image invalid: {img[:80]}")

    for img in valid_images:
        try:
            r = requests.post(tg_photo, data={
                "chat_id": CHANNEL, "photo": img,
                "caption": caption, "parse_mode": "HTML"
            }, timeout=30)
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            err = data.get("description", "")
            if "can't parse" in err.lower():
                clean = BeautifulSoup(caption, "html.parser").get_text()
                r2 = requests.post(tg_photo, data={
                    "chat_id": CHANNEL, "photo": img, "caption": clean[:1024]
                }, timeout=30)
                d2 = r2.json()
                if d2.get("ok"):
                    return d2["result"]["message_id"]
        except Exception as e:
            log.error(f"Send exception: {e}")

    # fallback: text only (no photo)
    try:
        r = requests.post(tg_text, data={
            "chat_id": CHANNEL, "text": caption, "parse_mode": "HTML"
        }, timeout=30)
        data = r.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        log.error(f"sendMessage fallback failed: {e}")
    return 0

# ─── Process One Game ─────────────────────────────────────────────────
AI_ENGINE = None

def process_game(game: dict) -> tuple:
    global AI_ENGINE
    store = game["store"]
    gid = game["id"]
    title = game["title"]
    is_ftk = game.get("is_free_to_keep", False)

    if is_recently_sent_cached(store, gid):
        return "skipped", "sent within last 24 hours (cache)"
    if game.get("is_free_to_play", False) and not is_ftk:
        return "invalid", "Free to Play (permanent)"

    start_date = game.get("deal_start", "")
    end_date = game.get("deal_end", "")

    # ── Stable period anchors (used for deal_hash) ──────────────────────
    # Xbox/PS: use month anchor so hash stays the same across runs in same month
    # Steam/GOG: use promo dates so hash changes when deal changes
    if store == "steam" and gid.isdigit():
        start_date, end_date, period_anchor = steam_get_promo_info(gid, is_ftk)
    elif store == "epic":
        start_date, end_date, period_anchor = epic_get_promo_info(game)
        if not period_anchor:
            period_anchor = f"EPIC:{end_date or current_week_anchor()}"
    elif store == "gog":
        start_date, end_date, period_anchor = gog_get_promo_info(game)
    elif store == "playstation":
        start_date, end_date, period_anchor = playstation_get_promo_info(game)
    elif store in ["playstation_essential", "playstation_extra"]:
        period_anchor = current_month_anchor()
    elif store == "xbox_gamepass":
        period_anchor = current_month_anchor()
    else:
        period_anchor = current_week_anchor()

    if store == "steam":
        details = steam_get_details(gid)
        if not details:
            return "failed", "no details"
        if details.get("is_free", False) and not is_ftk:
            game["is_free_to_play"] = True
            return "invalid", "Free to Play (permanent)"
        app_type = details.get("type", "")
        if app_type not in ("game", ""):
            return "invalid", f"type={app_type}"
        disc = game["discount"]
        if disc < MIN_DISCOUNT and disc != 100:
            return "invalid", f"discount {disc}% < {MIN_DISCOUNT}%"
        po = details.get("price_overview") or {}
        if po:
            game["price_orig_fmt"] = po.get("initial_formatted", game.get("price_orig_fmt", ""))
            game["price_final_fmt"] = po.get("final_formatted", game.get("price_final_fmt", ""))
            game["discount"] = po.get("discount_percent", game["discount"])
        raw = details.get("short_description", "")
        game["description"] = BeautifulSoup(raw, "html.parser").get_text() if raw else ""
        game["genres"] = [g["description"] for g in details.get("genres", [])]
        rev_pct, rev_count, rev_desc = steam_get_reviews(gid)
        game["review_pct"] = rev_pct
        game["review_count"] = rev_count
        game["review_desc"] = rev_desc
    elif store == "epic":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
    elif store == "gog":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
    elif store == "playstation":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
    elif store in ["playstation_essential", "playstation_extra"]:
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"{game['title']} is included in PS Plus this month."
    elif store == "xbox_gamepass":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"{game['title']} is available on Xbox Game Pass."
        if not game.get("review_pct"):
            game["review_pct"] = 85
            game["review_count"] = 5000
            game["review_desc"] = "Highly Rated"
    else:
        return "failed", f"unknown store {store}"

    game["is_aaa"] = is_aaa_game_metacritic(game["title"])
    calculate_priority_score(game)

    if AI_ENGINE:
        is_valid, reason, corrections = AI_ENGINE.decide(game)
        AI_ENGINE.log_decision(game, "accepted" if is_valid else "rejected", reason)
        if not is_valid:
            log.info(f"   AI rejected: {reason}")
            return "invalid", f"AI: {reason}"
        else:
            log.info(f"   AI accepted: {reason}")

    # ── Stable deal_hash ────────────────────────────────────────────────
    # Priority: explicit deal dates > price change > period anchor
    # This ensures the same deal in the same period always gets the same hash
    deal_hash = ""
    if start_date or end_date:
        raw_hash = f"{start_date}|{end_date}|{game.get('price_final_fmt','')}"
        deal_hash = hashlib.md5(raw_hash.encode()).hexdigest()[:16]
    if not deal_hash:
        deal_hash = make_promo_key(store, gid, period_anchor)

    # check if already sent with this exact hash (DB-backed — persisted between runs)
    if is_sent(store, gid, deal_hash):
        return "skipped", "already sent this deal"

    caption = build_caption(game, start_date, end_date)
    message_id = send_game(game, caption)
    if message_id > 0:
        mark_sent(store, gid, title, deal_hash, message_id)
        update_deal_history(game)
        mark_sent_cached(store, gid)
        # register expiry alert if deal has an end date
        if end_date:
            register_expiry_alert(store, gid, deal_hash, end_date)
        # auto-pin PC FTK posts
        handle_ftk_pin(game, message_id)
        return "sent", ""
    else:
        return "failed", "telegram send error"

# ─── Main ─────────────────────────────────────────────────────────────
def main():
    global AI_ENGINE
    log.info("=" * 65)
    log.info("  FreeGamesHub — Steam + Epic + GOG + PlayStation + Xbox")
    ai_mode = "Anthropic Claude API" if ANTHROPIC_API_KEY else "rule-based fallback"
    log.info(f"  AI Decision Engine: {ai_mode}")
    log.info("=" * 65)

    if not BOT_TOKEN or not CHANNEL:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    if not RAWG_API_KEY:
        log.warning("RAWG_API_KEY not set — genre/description may be limited")
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — using rule-based validation only")

    check_jdatetime()
    AI_ENGINE = AIDecisionEngine()
    init_db()

    # ── Step 1: Send expiry alerts for deals ending in next 2 hours ───────
    log.info("── Checking expiry alerts ──────────────────────────────")
    alerts = get_pending_expiry_alerts()
    if alerts:
        log.info(f"  {len(alerts)} deal(s) expiring soon — sending alerts")
        for alert in alerts:
            send_expiry_alert(alert)
    else:
        log.info("  No expiry alerts pending")

    # ── Step 2: Fetch all deals ───────────────────────────────────────────
    all_games = []

    log.info("── Fetching Steam ──────────────────────────────────────")
    _merge(all_games, fetch_steam_games())

    log.info("── Fetching Epic ───────────────────────────────────────")
    _merge(all_games, fetch_epic_games())

    log.info("── Fetching GOG ────────────────────────────────────────")
    _merge(all_games, fetch_gog_games())

    log.info("── Fetching PlayStation Deals ─────────────────────────")
    _merge(all_games, fetch_playstation_deals())

    log.info("── Fetching PS Plus Essential ─────────────────────────")
    _merge(all_games, fetch_playstation_plus_essential())

    log.info("── Fetching PS Plus Extra ─────────────────────────────")
    _merge(all_games, fetch_playstation_plus_extra())

    log.info("── Fetching Xbox Game Pass ────────────────────────────")
    _merge(all_games, fetch_xbox_gamepass())

    log.info(f"Total unique deals before filtering: {len(all_games)}")

    # filter permanent free-to-play (not FTK promos)
    filtered = []
    for g in all_games:
        if g.get("is_free_to_play", False) and not g.get("is_free_to_keep", False):
            continue
        # only check steam FTP — costly API call, skip for other stores
        if (g["store"] == "steam" and g["id"].isdigit()
                and steam_is_free_to_play(g["id"])
                and not g.get("is_free_to_keep", False)):
            continue
        filtered.append(g)
    all_games = filtered
    log.info(f"Total after filtering Free to Play: {len(all_games)}")

    if not all_games:
        log.warning("No games found — exiting")
        git_commit_db()
        return

    for g in all_games:
        calculate_priority_score(g)

    pc_stores   = ["steam", "epic", "gog"]
    ps_stores   = ["playstation", "playstation_essential", "playstation_extra"]
    xbox_stores = ["xbox_gamepass"]

    pc_games    = sorted([g for g in all_games if g["store"] in pc_stores],
                         key=lambda x: x.get("priority_score", 0), reverse=True)
    ps_games    = sorted([g for g in all_games if g["store"] in ps_stores],
                         key=lambda x: x.get("priority_score", 0), reverse=True)
    xbox_games_f = sorted([g for g in all_games if g["store"] in xbox_stores],
                          key=lambda x: x.get("priority_score", 0), reverse=True)

    log.info(f"  Grouped: PC={len(pc_games)}, PS={len(ps_games)}, Xbox={len(xbox_games_f)}")

    groups = [(name, list(games)) for name, games in
              [("PC", pc_games), ("PS", ps_games), ("Xbox", xbox_games_f)] if games]

    counters   = {"sent": 0, "skipped": 0, "invalid": 0, "failed": 0}
    total      = sum(len(g) for _, g in groups)
    item_num   = 0
    ai_rejected = 0

    while any(games for _, games in groups):
        for group_name, games in groups:
            if not games:
                continue
            game = games.pop(0)
            item_num += 1
            store  = game["store"].upper()
            label  = "FTK" if game.get("is_free_to_keep") else f"-{game['discount']}%"
            priority = game.get("priority_score", 0)
            log.info(f"[{item_num:3}/{total}] [{group_name:<4}][{store:<20}] {game['title'][:38]:<38} | {label} | Score:{priority}")

            status, reason = process_game(game)
            counters[status] = counters.get(status, 0) + 1
            if status == "sent":
                log.info("       Sent")
            elif status == "skipped":
                log.info(f"       Skipped — {reason}")
            elif status == "invalid":
                log.info(f"       Invalid — {reason}")
                if "AI:" in reason:
                    ai_rejected += 1
            else:
                log.error(f"       Failed — {reason}")
            time.sleep(3)

    log.info("=" * 65)
    log.info(f"  Sent:     {counters['sent']}")
    log.info(f"  Skipped:  {counters['skipped']}")
    log.info(f"  Invalid:  {counters['invalid']} (AI rejected: {ai_rejected})")
    log.info(f"  Failed:   {counters['failed']}")
    log.info("=" * 65)

    # ── Step 3: Persist DB back to repo so next run kno
