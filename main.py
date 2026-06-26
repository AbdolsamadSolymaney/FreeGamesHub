"""
FreeGamesHub — Steam + Epic + GOG + PlayStation + Xbox Game Pass
================================================================================
نسخه نهایی با اصلاحات:
- AI با endpoint صحیح GitHub Models
- GOG با اسکرپینگ + API
- PlayStation با Smart Selector + fallback
- تاریخ شمسی با پشتیبانی از فرمت‌های مختلف
- افزایش پایداری و retry
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
from typing import List, Dict, Any, Optional, Tuple
from bs4 import BeautifulSoup
import cloudscraper
import feedparser

# تلاش برای import jdatetime
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
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL      = os.environ.get("TELEGRAM_CHANNEL")
RAWG_API_KEY = os.environ.get("RAWG_API_KEY", "")
DB_FILE      = "games.db"
SELECTORS_DB = "selectors_cache.json"
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
        if (datetime.datetime.utcnow() - last_sent).total_seconds() < hours * 3600:
            return True
    return False

def mark_sent_cached(store: str, game_id: str):
    SENT_CACHE[(store, game_id)] = datetime.datetime.utcnow()

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
    conn.commit()
    conn.close()

def is_sent(store: str, game_id: str, deal_hash: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM sent WHERE store=? AND game_id=? AND deal_hash=?",
        (store, game_id, deal_hash)
    ).fetchone()
    conn.close()
    return row is not None

def mark_sent(store: str, game_id: str, title: str, deal_hash: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO sent (store, game_id, deal_hash, title, sent_at) VALUES (?,?,?,?,?)",
        (store, game_id, deal_hash, title, ts)
    )
    conn.commit()
    conn.close()

def make_promo_key(store: str, game_id: str, period_anchor: str) -> str:
    raw = f"{store}|{game_id}|{period_anchor}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def current_week_anchor() -> str:
    iso = datetime.datetime.utcnow().isocalendar()
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
        diff = (datetime.datetime.utcnow() - last).days
        return diff < days
    except:
        return False

# ─── HTTP Helper (با افزایش Retry) ────────────────────────────────────
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

# ─── AI Decision Engine (با endpoint صحیح) ────────────────────────────
class AIDecisionEngine:
    def __init__(self):
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.enabled = bool(self.token)
        self.consensus_count = 3
        self.confidence_threshold = 80
        if self.enabled:
            log.info("🤖 AI Decision Engine initialized with GitHub Models")
        else:
            log.warning("⚠️ GITHUB_TOKEN not set — using fallback logic")

    def decide(self, game: Dict[str, Any]) -> Tuple[bool, str, Dict]:
        if not self.enabled:
            return self._fallback_decision(game)
        quick = self._quick_check(game)
        if not quick[0]:
            return False, quick[1], {}
        results = []
        for _ in range(self.consensus_count):
            res = self._call_ai(game)
            if res:
                results.append(res)
            time.sleep(0.5)
        if not results:
            return self._fallback_decision(game)
        return self._analyze_consensus(results, game)

    def _quick_check(self, game: dict) -> Tuple[bool, str]:
        title = game.get('title', '')
        for kw in SKIP_KEYWORDS:
            if kw.lower() in title.lower():
                return False, f"Contains '{kw}'"
        if game.get('discount', 0) > 0:
            orig = game.get('price_orig_fmt', '')
            final = game.get('price_final_fmt', '')
            if orig and final and orig == final and game.get('discount', 0) > 0:
                return False, "Price mismatch"
        start = game.get('deal_start', '')
        end = game.get('deal_end', '')
        if start and end:
            try:
                s = datetime.datetime.strptime(start, "%Y-%m-%d")
                e = datetime.datetime.strptime(end, "%Y-%m-%d")
                if s > e:
                    return False, "Start after end"
            except:
                pass
        return True, "Quick check passed"

    def _call_ai(self, game: dict) -> Optional[dict]:
        prompt = self._build_prompt(game)
        try:
            # استفاده از endpoint صحیح GitHub Models (بدون /v1)
            response = requests.post(
                "https://models.github.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {"role": "system", "content": "You are a precise game deal validator. Always respond in JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.0,
                    "max_tokens": 500
                },
                timeout=15
            )
            if response.status_code != 200:
                log.warning(f"AI API error: {response.status_code}")
                return None
            data = response.json()
            content = data['choices'][0]['message']['content']
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return None
        except Exception as e:
            log.error(f"AI call error: {e}")
            return None

    def _build_prompt(self, game: dict) -> str:
        return f"""
You are an expert game deal validator AI. Your ONLY job is to analyze game deal data and decide if it's VALID and should be sent to users.

=== DATA ===
Title: {game.get('title', 'Unknown')}
Store: {game.get('store', 'Unknown')}
Discount: {game.get('discount', 0)}%
Original Price: {game.get('price_orig_fmt', 'N/A')}
Final Price: {game.get('price_final_fmt', 'N/A')}
Link: {game.get('link', 'N/A')}
Free to Keep: {game.get('is_free_to_keep', False)}
Start: {game.get('deal_start', 'N/A')}
End: {game.get('deal_end', 'N/A')}
Genres: {game.get('genres', [])}
Metacritic: {game.get('metacritic', 'N/A')}

=== RULES (REJECT IF ANY) ===
- Title contains DLC, Soundtrack, OST, Season Pass, Upgrade, Add-on, Deluxe, Bundle, Cosmetic
- Discount 100% but NOT Free to Keep
- Inconsistent prices (original=final but discount>0)
- Start > End
- Link invalid
- Title corrupted (special chars)

=== SPECIAL ===
- 100% + Free to Keep → ACCEPT
- Metacritic ≥ 90 → prioritize ACCEPT

=== RESPOND JSON ===
{{
  "decision": "accept" or "reject",
  "confidence": 0-100,
  "reason": "short explanation",
  "warnings": [],
  "corrected_data": {{}}
}}
"""

    def _analyze_consensus(self, results: list, game: dict) -> Tuple[bool, str, dict]:
        if len(results) < 2:
            return self._extract_result(results[0], game) if results else (False, "No AI response", {})
        accept = sum(1 for r in results if r.get("decision") == "accept")
        reject = len(results) - accept
        conf = sum(r.get("confidence", 0) for r in results) / len(results)
        if accept > reject and conf >= self.confidence_threshold:
            return True, f"Accepted ({accept}/{len(results)})", {}
        elif accept > reject and conf < self.confidence_threshold:
            return False, f"Low confidence ({conf:.0f}%)", {}
        else:
            return False, "Rejected by AI", {}

    def _extract_result(self, result: dict, game: dict) -> Tuple[bool, str, dict]:
        if result.get("decision") == "accept" and result.get("confidence", 0) >= self.confidence_threshold:
            return True, "Accepted", {}
        return False, "Rejected", {}

    def _fallback_decision(self, game: dict) -> Tuple[bool, str, dict]:
        title = game.get('title', '').lower()
        for kw in ["dlc", "soundtrack", "season pass", "deluxe", "bundle"]:
            if kw in title:
                return False, f"Contains '{kw}' (fallback)", {}
        if game.get('discount', 0) > 95 and not game.get('is_free_to_keep'):
            return False, "High discount but not Free to Keep (fallback)", {}
        return True, "Passed fallback validation", {}

    def log_decision(self, game: dict, decision: str, reason: str):
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            INSERT OR REPLACE INTO ai_decisions 
            (game_id, title, decision, confidence, warnings, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            game.get('id', 'unknown'),
            game.get('title', 'unknown'),
            decision,
            0,
            reason,
            datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()
        conn.close()

# ─── Date Formatter (با پشتیبانی از فرمت‌های مختلف) ──────────────────
def format_date_persian_english(date_str: str) -> str:
    if not date_str:
        return "نامشخص"
    date_str_clean = date_str.split("T")[0].split(" ")[0]
    formats = ["%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y"]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(date_str_clean, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return date_str
    gregorian = dt.strftime("%d %b %Y")
    if JDT_AVAILABLE and jdatetime:
        try:
            jd = jdatetime.datetime.fromgregorian(datetime=dt)
            persian = jd.strftime("%Y/%m/%d")
            return f"{persian}\n{gregorian}"
        except:
            return gregorian
    return gregorian

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
def get_image_candidates(game: dict) -> list[str]:
    candidates = []
    if game.get("steam_image"):
        candidates.append(game["steam_image"])
    if game.get("store") == "steam":
        candidates.append(f"https://cdn.cloudflare.steamstatic.com/steam/apps/{game['id']}/capsule_616x353.jpg")
        candidates.append(f"https://cdn.akamai.steamstatic.com/steam/apps/{game['id']}/header.jpg")
    if game.get("rawg_image"):
        candidates.append(game["rawg_image"])
    if game.get("image_url"):
        img = game["image_url"]
        img = img.replace("_small", "_large").replace("_thumb", "_original")
        img = re.sub(r'/\d+x\d+/', '/original/', img)
        candidates.append(img)
        candidates.append(game["image_url"])
    return candidates

# ─── Priority Score ────────────────────────────────────────────────────
def calculate_priority_score(game: dict) -> int:
    score = 0
    if game.get("review_pct"):
        score += min(int(game["review_pct"] / 4), 25)
    elif game.get("rawg_rating"):
        score += min(int(game.get("rawg_rating", 0) * 5), 25)
    if game.get("metacritic"):
        mc = game.get("metacritic", 0)
        if mc >= 90:
            score += 15
        elif mc >= 80:
            score += 10
        elif mc >= 70:
            score += 5
    if game.get("review_count"):
        cnt = game.get("review_count", 0)
        if cnt > 100000:
            score += 15
        elif cnt > 50000:
            score += 10
        elif cnt > 10000:
            score += 5
        elif cnt > 1000:
            score += 3
    discount = game.get("discount", 0)
    if discount == 100:
        score += 10
    elif discount >= 90:
        score += 8
    elif discount >= 80:
        score += 5
    elif discount >= 75:
        score += 3
    if game.get("is_aaa"):
        score += 15
    if game.get("release_date"):
        try:
            rel = datetime.datetime.fromisoformat(game["release_date"].replace("Z", "+00:00")).replace(tzinfo=None)
            days = (datetime.datetime.utcnow() - rel).days
            if days < 30:
                score += 10
            elif days < 90:
                score += 5
            elif days < 365:
                score += 2
        except:
            pass
    if game.get("is_free_to_keep"):
        score += 20
    if game.get("is_free_to_play"):
        score = -999
    game["priority_score"] = score
    return score

# ─── Deal Type ──────────────────────────────────────────────────────────
def detect_deal_type(game: dict) -> str:
    if game.get("is_free_to_play"):
        return "free_to_play"
    if game.get("is_free_to_keep"):
        return "free_to_keep"
    if game.get("discount") == 100:
        return "free_weekend"
    return "discount"

def get_deal_emoji(deal_type: str) -> str:
    return {"free_to_keep": "🟢", "free_weekend": "🟡", "free_to_play": "🔵"}.get(deal_type, "🟣")

def get_deal_label(deal_type: str, discount: int) -> str:
    if deal_type == "free_to_keep":
        return "Free to Keep (برای همیشه)"
    elif deal_type == "free_weekend":
        return "Free Weekend (آخر هفته رایگان)"
    elif deal_type == "free_to_play":
        return "Free to Play (کاملاً رایگان)"
    else:
        return f"{discount}% تخفیف"

# ─── Steam Sources ─────────────────────────────────────────────────────
def _steam_fetch_featured() -> list[dict]:
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
        for section in ["top_sellers", "new_releases"]:
            for item in data.get(section, {}).get("items", []):
                disc = item.get("discount_percent", 0)
                if disc < MIN_DISCOUNT and disc != 100:
                    continue
                name = item.get("name", "")
                appid = str(item.get("id", ""))
                if not appid or not name:
                    continue
                orig = item.get("original_price", 0)
                final = item.get("final_price", 0)
                games.append(make_game("steam", appid, name, disc,
                    f"https://store.steampowered.com/app/{appid}/",
                    f"${orig/100:.2f}" if orig else "",
                    f"${final/100:.2f}" if final else "",
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"))
    except Exception as e:
        log.error(f"Steam Featured error: {e}")
    return games

def _steam_fetch_html_search() -> list[dict]:
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

def _steam_fetch_free_to_keep() -> list[dict]:
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

def steam_get_promo_info(appid: str, is_ftk: bool) -> tuple[str, str, str]:
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

def fetch_steam_games() -> list[dict]:
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

def fetch_epic_games() -> list[dict]:
    games = []
    r = safe_get(EPIC_GQL_URL, params={"locale": "en-US", "country": "US", "allowCountries": "US"},
                 extra_headers={"Referer": "https://store.epicgames.com/"})
    if not r:
        log.error("Epic API failed")
        return games
    try:
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
        now = datetime.datetime.utcnow()
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

def epic_get_promo_info(game: dict) -> tuple[str, str, str]:
    r = safe_get(EPIC_GQL_URL, params={"locale": "en-US", "country": "US", "allowCountries": "US"},
                 extra_headers={"Referer": "https://store.epicgames.com/"})
    if not r:
        return "", "", f"FTK:{current_week_anchor()}"
    try:
        data = r.json()
        elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
        now = datetime.datetime.utcnow()
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

# ─── GOG (API + اسکرپینگ) ─────────────────────────────────────────────
def fetch_gog_games() -> list[dict]:
    games = []
    log.info("  🔍 Fetching GOG via API (official)")
    api_games = _gog_fetch_api()
    _merge(games, api_games)
    log.info(f"  GOG API: {len(api_games)} games")
    if len(games) < 5:
        log.info("  🔍 Fetching GOG via Scraping")
        scrape_games = _gog_fetch_scrape()
        before = len(games)
        _merge(games, scrape_games)
        log.info(f"  GOG Scrape: {len(scrape_games)} raw → {len(games)-before} new")
    log.info(f"  ✅ GOG total: {len(games)} games found")
    return games

def _gog_fetch_api() -> list[dict]:
    games = []
    url = "https://catalog.gog.com/v1/catalog"
    params = {
        "limit": 100,
        "order": "desc:trending",
        "discounted": "true",
        "productType": "in:game",
        "page": 1,
        "countryCode": "US",
        "currencyCode": "USD",
        "price": "0,1000"
    }
    r = safe_get(url, params=params, use_scraper=True, retries=3, extra_headers={"Referer": "https://www.gog.com/", "Accept": "application/json"})
    if not r:
        log.warning("  ⚠️ GOG API failed")
        return games
    try:
        data = r.json()
        products = data.get("products", [])
        log.info(f"  📊 GOG API returned {len(products)} products")
        for item in products:
            title = item.get("title", "").strip()
            if not title:
                continue
            price_info = item.get("price", {})
            if not price_info:
                continue
            discount = int(price_info.get("discountPercentage", 0) or 0)
            if discount < MIN_DISCOUNT and discount != 100:
                continue
            base_price = price_info.get("base", 0)
            final_price = price_info.get("final", 0)
            orig_fmt = f"${float(base_price):.2f}" if base_price else ""
            final_fmt = f"${float(final_price):.2f}" if final_price else ("FREE" if discount == 100 else "")
            slug = item.get("slug", "")
            link = f"https://www.gog.com/en/game/{slug}" if slug else "https://www.gog.com"
            cover = item.get("coverHorizontal", "") or item.get("cover", "")
            game_id = str(item.get("id", slug or title))
            is_ftk = (discount == 100 and final_price == 0)
            game = make_game("gog", game_id, title, discount, link, orig_fmt, final_fmt, image_url=cover, is_free_to_keep=is_ftk)
            games.append(game)
            log.info(f"  🟣 GOG API: {title} -{discount}%")
    except Exception as e:
        log.warning(f"  ⚠️ GOG API parse error: {e}")
    return games

def _gog_fetch_scrape() -> list[dict]:
    games = []
    urls = [
        "https://www.gog.com/en/games?discounted=true&page=1",
        "https://www.gog.com/en/games?discounted=true&page=2"
    ]
    r = None
    for url in urls:
        r = safe_get(url, use_scraper=True, retries=3, extra_headers={"Referer": "https://www.gog.com/"})
        if r:
            break
    if not r:
        log.warning("  ⚠️ GOG scrape failed")
        return games
    soup = BeautifulSoup(r.text, "html.parser")
    # سلکتورهای جدید
    products = soup.select("[data-testid='productCard']") or soup.select(".product-tile") or soup.select(".product-card")
    if not products:
        products = soup.select("a[href*='/en/game/']")
    log.info(f"    Found {len(products)} potential products")
    for prod in products[:50]:
        try:
            title_el = prod.select_one("[data-testid='productTitle']") or prod.select_one(".product-title") or prod.select_one(".title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or _should_skip(title):
                continue
            discount_el = prod.select_one("[data-testid='discountPercentage']") or prod.select_one(".discount-percentage")
            discount = 0
            if discount_el:
                disc_text = discount_el.get_text(strip=True).replace("%", "").replace("-", "")
                try:
                    discount = int(disc_text)
                except:
                    pass
            if discount == 0:
                price_el = prod.select_one(".final-price, .price")
                if price_el and "free" in price_el.get_text(strip=True).lower():
                    discount = 100
            if discount < MIN_DISCOUNT and discount != 100:
                continue
            orig_el = prod.select_one(".original-price, .old-price")
            final_el = prod.select_one(".final-price, .current-price")
            orig = orig_el.get_text(strip=True) if orig_el else ""
            final = final_el.get_text(strip=True) if final_el else ""
            link_el = prod.select_one("a[href*='/en/game/']")
            link = link_el.get("href", "") if link_el else ""
            if link and not link.startswith("http"):
                link = "https://www.gog.com" + link
            if not link or "/en/game/" not in link:
                continue
            img_el = prod.select_one("img")
            image_url = img_el.get("src") or img_el.get("data-src", "") if img_el else ""
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url
            game_id = re.search(r"/game/([^/?]+)", link)
            game_id = game_id.group(1) if game_id else title.lower().replace(" ", "-").replace(":", "")
            is_ftk = (discount == 100)
            game = make_game("gog", game_id, title, discount, link, orig, final, image_url, is_free_to_keep=is_ftk)
            games.append(game)
            log.info(f"  🟣 GOG Scrape: {title} -{discount}%")
        except Exception as e:
            continue
    return games

def gog_get_promo_info(game: dict) -> tuple[str, str, str]:
    r = safe_get(game["link"], retries=2, extra_headers={"Referer": "https://www.gog.com/"})
    start = ""
    end = ""
    if r:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("[data-test-id='discountCountdown'], .discount-countdown"):
                text = el.get_text(strip=True)
                s = re.search(r"Starts\s+(\d{4}-\d{2}-\d{2})", text, re.IGNORECASE)
                if s:
                    start = s.group(1)
                e = re.search(r"Ends\s+(\d{4}-\d{2}-\d{2})", text, re.IGNORECASE)
                if e:
                    end = e.group(1)
        except:
            pass
    week = current_week_anchor()
    prefix = "FTK:" if game.get("is_free_to_keep") else ""
    return start, end, f"{prefix}{week}"

# ─── PlayStation (Smart Selector + fallback) ──────────────────────────
class SmartSelector:
    def __init__(self, soup: BeautifulSoup, store: str, page_type: str):
        self.soup = soup
        self.store = store
        self.page_type = page_type
        self.cache_key = f"{store}_{page_type}"
        self.selector_cache = self._load_cache()

    def _load_cache(self):
        try:
            with open(SELECTORS_DB, "r") as f:
                return json.load(f)
        except:
            return {}

    def _save_cache(self, cache):
        try:
            with open(SELECTORS_DB, "w") as f:
                json.dump(cache, f, indent=2)
        except:
            pass

    def find_elements(self, target: str, parent=None) -> list:
        soup_target = parent if parent else self.soup
        cached = self.selector_cache.get(self.cache_key, {}).get(f"{target}_list")
        if cached:
            try:
                res = soup_target.select(cached)
                if res:
                    return res
            except:
                pass
        defaults = self._get_default_selectors(f"{target}_list")
        for sel in defaults:
            try:
                res = soup_target.select(sel)
                if res:
                    self.selector_cache.setdefault(self.cache_key, {})[f"{target}_list"] = sel
                    self._save_cache(self.selector_cache)
                    return res
            except:
                continue
        return []

    def _get_default_selectors(self, target: str) -> list:
        base = {
            "game_card_list": [
                "[data-testid='product-card']", ".product-card", ".product-tile",
                "[class*='product']", "li[data-product-id]", "[class*='game']",
                ".game-item", ".game-tile", "a[href*='/product/']", ".card", ".item"
            ]
        }
        store_specific = {
            "playstation": {
                "game_card_list": ["[data-testid='product-card']", ".product-card", ".grid-cell", ".card"]
            }
        }
        all_sel = base.get(target, [])
        if self.store in store_specific:
            all_sel = store_specific[self.store].get(target, []) + all_sel
        return all_sel

    def get_text_safe(self, el, default=""):
        return el.get_text(strip=True) if el else default

    def get_attr_safe(self, el, attr, default=""):
        return el.get(attr, default) if el else default

    def find_element(self, target: str, parent=None):
        return None  # not used in PlayStation fetch

def fetch_playstation_deals() -> list[dict]:
    games = []
    urls = [
        "https://store.playstation.com/en-us/pages/deals",
        "https://store.playstation.com/en-us/deals"
    ]
    r = None
    for url in urls:
        r = safe_get(url, use_scraper=True, retries=3, extra_headers={"Referer": "https://store.playstation.com/"})
        if r:
            break
    if not r:
        log.error("  ❌ All PlayStation deals pages failed")
        return games
    soup = BeautifulSoup(r.text, "html.parser")
    selector = SmartSelector(soup, "playstation", "deals")
    products = selector.find_elements("game_card_list")
    log.info(f"  🔍 Found {len(products)} products using Smart Selector")
    for prod in products[:50]:
        try:
            title_el = prod.select_one(".product-title, [data-testid='product-title'], h3, .title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or _should_skip(title):
                continue
            discount_el = prod.select_one("[class*='discount'], .discount-badge")
            discount = 0
            if discount_el:
                disc_text = discount_el.get_text(strip=True)
                match = re.search(r'(\d+)%', disc_text)
                if match:
                    discount = int(match.group(1))
            if discount < MIN_DISCOUNT:
                continue
            link_el = prod.select_one("a[href*='/product/']")
            link = link_el.get("href", "") if link_el else ""
            if link and not link.startswith("http"):
                link = "https://store.playstation.com" + link
            img_el = prod.select_one("img")
            image_url = img_el.get("src") or img_el.get("data-src", "") if img_el else ""
            if image_url and image_url.startswith("//"):
                image_url = "https:" + image_url
            if image_url:
                image_url = image_url.replace("_small", "_large").replace("_thumb", "_original")
            game_id = re.search(r"/product/([^/?]+)", link)
            game_id = game_id.group(1) if game_id else title.lower().replace(" ", "-").replace(":", "")
            orig_el = prod.select_one(".price__old, .original-price")
            final_el = prod.select_one(".price__current, .final-price")
            orig = orig_el.get_text(strip=True) if orig_el else ""
            final = final_el.get_text(strip=True) if final_el else ""
            game = make_game("playstation", game_id, title, discount, link, orig, final, image_url)
            games.append(game)
            log.info(f"  🎮 PS Deal: {title} -{discount}%")
        except:
            continue
    # اگر هیچی پیدا نشد، fallback به psdeals.net
    if not games:
        log.info("  🔍 Fallback to psdeals.net")
        r2 = safe_get("https://psdeals.net/us-store/deals", use_scraper=True, retries=3)
        if r2:
            soup2 = BeautifulSoup(r2.text, "html.parser")
            for item in soup2.select(".game-item, .deal-item"):
                try:
                    title_el = item.select_one(".title, .name")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    if not title or _should_skip(title):
                        continue
                    disc_el = item.select_one(".discount, .price-off")
                    discount = 0
                    if disc_el:
                        disc_text = disc_el.get_text(strip=True)
                        match = re.search(r'(\d+)%', disc_text)
                        if match:
                            discount = int(match.group(1))
                    if discount < MIN_DISCOUNT:
                        continue
                    link_el = item.select_one("a[href]")
                    link = link_el.get("href", "") if link_el else ""
                    if link and not link.startswith("http"):
                        link = "https://psdeals.net" + link
                    img_el = item.select_one("img")
                    image_url = img_el.get("src") or img_el.get("data-src", "") if img_el else ""
                    if image_url and image_url.startswith("//"):
                        image_url = "https:" + image_url
                    game_id = title.lower().replace(" ", "-").replace(":", "")
                    game = make_game("playstation", game_id, title, discount, link, "", "", image_url)
                    games.append(game)
                    log.info(f"  🎮 PS Deal (fallback): {title} -{discount}%")
                except:
                    continue
    log.info(f"  ✅ PlayStation deals: {len(games)} games found")
    return games

def playstation_get_promo_info(game: dict) -> tuple[str, str, str]:
    if not game.get("link"):
        return "", "", f"END:{current_week_anchor()}"
    r = safe_get(game["link"], retries=2, use_scraper=True, extra_headers={"Referer": "https://store.playstation.com/"})
    start = ""
    end = ""
    if r:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select(".offer-start, .offer-end"):
                text = el.get_text(strip=True)
                match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
                if match:
                    dt = datetime.datetime.strptime(match.group(1), "%m/%d/%Y")
                    if "start" in el.get("class", []):
                        start = dt.strftime("%Y-%m-%d")
                    else:
                        end = dt.strftime("%Y-%m-%d")
        except:
            pass
    return start, end, f"END:{current_week_anchor()}"

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
                "dead space","mass effect","dragon age","batman","arkham","tomb raider","wolfenstein"]
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

# ─── PS Plus Essential / Extra ──────────────────────────────────────
def fetch_playstation_plus_essential() -> list[dict]:
    games = []
    log.info("  🔍 Fetching PS Plus Essential from official PlayStation Blog RSS")
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:30]:
            if "PlayStation Plus" not in entry.title and "PS Plus" not in entry.title:
                continue
            content = entry.content[0].value if hasattr(entry, 'content') else entry.description
            full = entry.title + " " + content
            pattern = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*(?:,?\s*and\s*|\s*&\s*)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'
            matches = re.findall(pattern, full)
            if matches:
                for match in matches[0]:
                    name = match.strip()
                    if len(name) > 3 and not any(x in name.lower() for x in ["plus", "playstation", "member", "download", "month"]):
                        gid = name.lower().replace(" ", "-").replace(":", "").replace("'", "")
                        games.append(make_game("playstation_essential", gid, name, 100,
                            entry.link, "", "FREE (PS Plus Essential)", "", is_free_to_keep=True))
                if games:
                    log.info(f"    Found {len(games)} games from RSS")
                    return games
            famous = ["God of War", "Horizon", "Uncharted", "The Last of Us", "Final Fantasy",
                      "Resident Evil", "Cyberpunk", "The Witcher", "Red Dead", "GTA",
                      "Assassin's Creed", "Far Cry", "Ghost of Tsushima", "Death Stranding",
                      "Days Gone", "Demon's Souls", "Ratchet and Clank", "Returnal",
                      "Spider-Man", "Diablo", "Overwatch", "Doom", "Fallout", "Elder Scrolls",
                      "Minecraft", "Age of Empires"]
            for game in famous:
                if game.lower() in full.lower():
                    gid = game.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    games.append(make_game("playstation_essential", gid, game, 100,
                        entry.link, "", "FREE (PS Plus Essential)", "", is_free_to_keep=True))
    except Exception as e:
        log.warning(f"  ⚠️ RSS error: {e}")
    if not games:
        log.info("  🔍 Fallback: using static list")
        for title, gid in [("God of War Ragnarök", "god-of-war-ragnarok"),
                           ("The Last of Us Part I", "the-last-of-us-part-i"),
                           ("Final Fantasy VII Remake", "final-fantasy-vii-remake")]:
            games.append(make_game("playstation_essential", gid, title, 100,
                "https://blog.playstation.com", "", "FREE (PS Plus Essential)", "", is_free_to_keep=True))
    log.info(f"  ✅ PS Essential: {len(games)} games found")
    return games

def fetch_playstation_plus_extra() -> list[dict]:
    games = []
    log.info("  🔍 Fetching PS Plus Extra from official PlayStation Blog RSS")
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:30]:
            if "PS Plus Extra" not in entry.title and "PlayStation Plus Extra" not in entry.title:
                continue
            content = entry.content[0].value if hasattr(entry, 'content') else entry.description
            full = entry.title + " " + content
            aaa = ["God of War", "Horizon", "Uncharted", "The Last of Us", "Final Fantasy",
                   "Resident Evil", "Cyberpunk", "The Witcher", "Red Dead", "GTA",
                   "Assassin's Creed", "Far Cry", "Ghost of Tsushima", "Death Stranding",
                   "Days Gone", "Demon's Souls", "Ratchet and Clank", "Returnal",
                   "Spider-Man", "Diablo", "Overwatch", "Doom", "Fallout", "Elder Scrolls",
                   "Minecraft", "Age of Empires", "Dead Space", "Mass Effect", "Dragon Age", "Batman"]
            for game in aaa:
                if game.lower() in full.lower():
                    gid = game.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    games.append(make_game("playstation_extra", gid, game, 0,
                        entry.link, "", "Included in PS Plus Extra", "", is_free_to_keep=False))
    except Exception as e:
        log.warning(f"  ⚠️ RSS Extra error: {e}")
    if not games:
        log.info("  🔍 Fallback: using static list")
        for title, gid in [("God of War Ragnarök", "god-of-war-ragnarok"),
                           ("Horizon Forbidden West", "horizon-forbidden-west"),
                           ("The Last of Us Part I", "the-last-of-us-part-i")]:
            games.append(make_game("playstation_extra", gid, title, 0,
                "https://blog.playstation.com", "", "Included in PS Plus Extra", "", is_free_to_keep=False))
    log.info(f"  ✅ PS Extra: {len(games)} games found")
    return games

# ─── Xbox Game Pass ────────────────────────────────────────────────────
def fetch_xbox_gamepass() -> list[dict]:
    games = []
    log.info("  🔍 Fetching Xbox Game Pass with Metacritic detection")
    current_games = [
        ("Starfield", "starfield", 1716740),
        ("Forza Horizon 5", "forza-horizon-5", 1551360),
        ("Halo Infinite", "halo-infinite", 1240440),
        ("Call of Duty", "call-of-duty", 1938090),
        ("Diablo IV", "diablo-iv", 2344520),
        ("Overwatch 2", "overwatch-2", 2357570),
        ("Doom Eternal", "doom-eternal", 782330),
        ("Fallout 4", "fallout-4", 377160),
        ("The Elder Scrolls V: Skyrim", "skyrim", 489830),
        ("Minecraft", "minecraft", 1151280),
        ("Age of Empires IV", "age-of-empires-iv", 1466860),
        ("Gears 5", "gears-5", 1097840),
        ("Dead Space", "dead-space", 1693980),
        ("Mass Effect Legendary Edition", "mass-effect-legendary", 1328670),
        ("Batman: Arkham Knight", "batman-arkham-knight", 208650),
        ("Star Wars Jedi: Survivor", "jedi-survivor", 1774580),
        ("Sniper Elite 5", "sniper-elite-5", 1039690),
        ("Mafia: Definitive Edition", "mafia", 1030840),
        ("Crisis Core: Final Fantasy VII", "crisis-core", 1852400),
        ("Dragon's Dogma 2", "dragons-dogma-2", 2054970),
        ("Devil May Cry 5", "devil-may-cry-5", 601150),
        ("Monster Hunter Rise", "monster-hunter-rise", 1446780),
        ("Persona 5 Royal", "persona-5-royal", 1687950),
        ("Yakuza: Like a Dragon", "yakuza-like-a-dragon", 1235140),
        ("Dragon Quest XI", "dragon-quest-xi", 1295510),
        ("Borderlands 3", "borderlands-3", 397540),
        ("Bioshock: The Collection", "bioshock-collection", 409710),
        ("Dying Light 2", "dying-light-2", 534380),
        ("Just Cause 4", "just-cause-4", 517630),
        ("Sleeping Dogs", "sleeping-dogs", 202170),
    ]
    for title, slug, steam_id in current_games:
        if not is_aaa_game_metacritic(title):
            log.debug(f"  ⏭️ Skipping {title} (not AAA)")
            continue
        if steam_id > 0 and steam_is_free_to_play(str(steam_id)):
            log.debug(f"  ⏭️ Skipping {title} (Free to Play)")
            continue
        image_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{steam_id}/capsule_616x353.jpg"
        if steam_id == 0:
            rawg = rawg_search(title)
            if rawg and rawg.get("background_image"):
                image_url = rawg["background_image"]
        game = make_game("xbox_gamepass", slug, title, 0,
            "https://www.xbox.com/en-US/xbox-game-pass", "", "Included in Game Pass", image_url, is_free_to_keep=False)
        games.append(game)
        log.info(f"  🟩 Xbox Game Pass (AAA): {title}")
    log.info(f"  ✅ Xbox Game Pass: {len(games)} AAA games")
    return games

# ─── Steam Search Enrich ──────────────────────────────────────────────
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
            review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} RAWG ratings"
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

    tags = ["#FreeGamesHub", meta["tag"]]
    for g in genres[:2]:
        tag = re.sub(r'[^a-zA-Z0-9]', '', g)
        if tag:
            tags.append(f"#{tag}")
    if is_ftk:
        tags.append("#FreeToKeep")
    elif discount == 100:
        tags.append("#FreeGames")
    if discount >= 75:
        tags.append("#BigDeal")
    if discount >= 90:
        tags.append("#MegaDeal")
    if game.get("is_aaa"):
        tags.append("#AAA")
    hashtags = " ".join(tags)

    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    lines = [
        f"{meta['emoji']} <b>[{meta['name']}]</b>  🎮 <b>{title}</b>",
        "",
    ]
    if genre_str:
        lines += [f"🎯 <b>Genre:</b> {genre_str}", ""]
    if desc and desc != "No description available.":
        lines += ["📝 <b>About:</b>", desc, ""]
    if review_line:
        label = "Steam Reviews" if store == "steam" else "Rating"
        lines += [f"⭐ <b>{label}:</b> {review_line}", ""]
    lines += [
        f"💰 <b>Price:</b> {price_block}",
        f"💸 <b>Discount:</b> {disc_block}",
        "",
    ]
    if start_date or end_date:
        lines += ["📅 <b>Offer Calendar:</b>", ""]
        if start_date:
            lines += [f"  🟢 <b>Start:</b> {format_date_persian_english(start_date)}", ""]
        if end_date:
            lines += [f"  🔴 <b>End:</b> {format_date_persian_english(end_date)}", ""]
    lines += [
        f"📅 <b>Detected:</b> {now_utc} UTC",
        "",
        f"🔗 {game['link']}",
        "",
        hashtags,
    ]
    caption = "\n".join(lines)
    if len(caption) > 1024:
        short = raw_desc[:80].rstrip() + "…"
        new_lines = []
        for line in lines:
            if line.startswith("📝 <b>About:</b>"):
                new_lines.append(f"📝 <b>About:</b> {short}")
                continue
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
        caption = "\n".join(new_lines)[:1024]
    return caption

# ─── Telegram Sender ──────────────────────────────────────────────────
def send_game(game: dict, caption: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    candidates = get_image_candidates(game)
    candidates = [c for c in candidates if c]
    for img in candidates:
        try:
            r = requests.post(url, data={"chat_id": CHANNEL, "photo": img, "caption": caption, "parse_mode": "HTML"}, timeout=30)
            if r.json().get("ok"):
                return True
            err = r.json().get("description", "")
            log.warning(f"Telegram error: {err}")
            if "can't parse" in err.lower():
                clean = BeautifulSoup(caption, "html.parser").get_text()
                r2 = requests.post(url, data={"chat_id": CHANNEL, "photo": img, "caption": clean[:1024]}, timeout=30)
                if r2.json().get("ok"):
                    return True
            if "wrong type" in err or "failed" in err:
                continue
        except Exception as e:
            log.error(f"Send exception: {e}")
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML"}, timeout=30)
        if r.json().get("ok"):
            return True
    except Exception as e:
        log.error(f"sendMessage fallback failed: {e}")
    return False

# ─── Process One Game ─────────────────────────────────────────────────
AI_ENGINE = None

def process_game(game: dict) -> tuple[str, str]:
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
    period_anchor = ""

    if store == "steam" and gid.isdigit():
        start_date, end_date, period_anchor = steam_get_promo_info(gid, is_ftk)
    elif store == "epic":
        start_date, end_date, period_anchor = epic_get_promo_info(game)
    elif store == "gog":
        start_date, end_date, period_anchor = gog_get_promo_info(game)
    elif store == "playstation":
        start_date, end_date, period_anchor = playstation_get_promo_info(game)
    elif store in ["playstation_essential", "playstation_extra"]:
        period_anchor = f"MONTH:{datetime.datetime.utcnow().strftime('%Y-%m')}"
    elif store == "xbox_gamepass":
        period_anchor = f"GAMEPASS:{datetime.datetime.utcnow().strftime('%Y-%m')}"
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
            game["description"] = f"🎮 {game['title']} is included in PS Plus!"
    elif store == "xbox_gamepass":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"🎮 {game['title']} is available on Xbox Game Pass!"
        if not game.get("review_pct"):
            game["review_pct"] = 85
            game["review_count"] = 5000
            game["review_desc"] = "Highly Rated"
        if is_recently_sent_db(store, gid, days=365):
            return "skipped", "sent within last year"
    else:
        return "failed", f"unknown store {store}"

    game["is_aaa"] = is_aaa_game_metacritic(game["title"])
    calculate_priority_score(game)

    if AI_ENGINE:
        is_valid, reason, corrections = AI_ENGINE.decide(game)
        if corrections:
            for k, v in corrections.items():
                if k in game:
                    game[k] = v
        AI_ENGINE.log_decision(game, "accepted" if is_valid else "rejected", reason)
        if not is_valid:
            log.info(f"   🤖 AI rejected: {reason}")
            return "invalid", f"AI: {reason}"
        else:
            log.info(f"   🤖 AI accepted: {reason}")

    deal_hash = game.get("deal_hash", "")
    if not deal_hash:
        deal_hash = get_deal_hash(game)
    if not deal_hash:
        deal_hash = make_promo_key(store, gid, period_anchor)

    if not is_deal_changed(store, gid, deal_hash):
        return "skipped", "no change in deal"
    if is_sent(store, gid, deal_hash):
        return "skipped", "already sent this deal"

    caption = build_caption(game, start_date, end_date)
    ok = send_game(game, caption)
    if ok:
        mark_sent(store, gid, title, deal_hash)
        mark_sent_cached(store, gid)
        return "sent", ""
    else:
        return "failed", "telegram send error"

# ─── Main ─────────────────────────────────────────────────────────────
def main():
    global AI_ENGINE
    log.info("═" * 65)
    log.info("  🎮 FreeGamesHub — Steam + Epic + GOG + PlayStation + Xbox")
    log.info("  🤖 AI Decision Engine: ENABLED")
    log.info("═" * 65)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    if not RAWG_API_KEY:
        log.warning("⚠️  RAWG_API_KEY not set — some stores may lack genre/description")
    if not JDT_AVAILABLE:
        log.warning("⚠️  jdatetime not installed — Persian dates will not be shown. Install with: pip install jdatetime")

    AI_ENGINE = AIDecisionEngine()
    init_db()

    all_games = []

    log.info("── Fetching Steam ──────────────────────────────────────")
    steam_games = fetch_steam_games()
    _merge(all_games, steam_games)

    log.info("── Fetching Epic ───────────────────────────────────────")
    epic_games = fetch_epic_games()
    _merge(all_games, epic_games)

    log.info("── Fetching GOG ────────────────────────────────────────")
    gog_games = fetch_gog_games()
    _merge(all_games, gog_games)

    log.info("── Fetching PlayStation Deals ─────────────────────────")
    ps_deals = fetch_playstation_deals()
    _merge(all_games, ps_deals)

    log.info("── Fetching PS Plus Essential ─────────────────────────")
    ps_essential = fetch_playstation_plus_essential()
    _merge(all_games, ps_essential)

    log.info("── Fetching PS Plus Extra ─────────────────────────────")
    ps_extra = fetch_playstation_plus_extra()
    _merge(all_games, ps_extra)

    log.info("── Fetching Xbox Game Pass ────────────────────────────")
    xbox_games = fetch_xbox_gamepass()
    _merge(all_games, xbox_games)

    log.info(f"Total unique deals before filtering: {len(all_games)}")

    # فیلتر Free to Play
    filtered = []
    for g in all_games:
        if g.get("is_free_to_play", False) and not g.get("is_free_to_keep", False):
            continue
        if g["store"] == "steam" and g["id"].isdigit() and steam_is_free_to_play(g["id"]) and not g.get("is_free_to_keep", False):
            continue
        filtered.append(g)
    all_games = filtered
    log.info(f"Total after filtering Free to Play: {len(all_games)}")

    if not all_games:
        log.warning("No games found — exiting")
        return

    for g in all_games:
        calculate_priority_score(g)

    # گروه‌بندی
    pc_stores = ["steam", "epic", "gog"]
    ps_stores = ["playstation", "playstation_essential", "playstation_extra"]
    xbox_stores = ["xbox_gamepass"]

    pc_games = [g for g in all_games if g["store"] in pc_stores]
    ps_games = [g for g in all_games if g["store"] in ps_stores]
    xbox_games_f = [g for g in all_games if g["store"] in xbox_stores]

    pc_games.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    ps_games.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
    xbox_games_f.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

    log.info(f"  📊 Grouped: PC={len(pc_games)}, PS={len(ps_games)}, Xbox={len(xbox_games_f)}")

    groups = [
        ("PC", pc_games),
        ("PS", ps_games),
        ("Xbox", xbox_games_f),
    ]
    groups = [(name, games) for name, games in groups if games]

    counters = {"sent": 0, "skipped": 0, "invalid": 0, "failed": 0}
    total = sum(len(g) for _, g in groups)
    sent = 0
    ai_rejected = 0

    while sent < total:
        for group_name, games in groups:
            if not games:
                continue
            game = games.pop(0)
            sent += 1
            store = game["store"].upper()
            label = "🎁 FTK" if game.get("is_free_to_keep") else f"-{game['discount']}%"
            priority = game.get("priority_score", 0)
            log.info(f"[{sent:3}/{total}] [{group_name:<4}][{store:<5}] {game['title'][:40]:<40} | {label} | Score:{priority}")

            status, reason = process_game(game)
            counters[status] = counters.get(status, 0) + 1
            if status == "sent":
                log.info("       ✅ Sent")
            elif status == "skipped":
                log.info(f"       ⏭  Skipped — {reason}")
            elif status == "invalid":
                log.info(f"       ⚠️  Invalid — {reason}")
                if "AI:" in reason:
                    ai_rejected += 1
            else:
                log.error(f"       ❌ Failed — {reason}")
            time.sleep(3)

    log.info("═" * 65)
    log.info(f"  ✅ Sent:     {counters['sent']}")
    log.info(f"  ⏭  Skipped: {counters['skipped']}")
    log.info(f"  ⚠️  Invalid: {counters['invalid']} (AI rejected: {ai_rejected})")
    log.info(f"  ❌ Failed:   {counters['failed']}")
    log.info("═" * 65)

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            log.exception("❗ خطای غیرمنتظره در حین اجرا:")
        log.info("⏳ منتظر ۱۲ ساعت تا اجرای بعدی...")
        time.sleep(12 * 3600)
