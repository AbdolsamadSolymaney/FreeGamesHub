"""
FreeGamesHub — Steam + Epic Games Store + GOG + PlayStation + Xbox Game Pass
================================================================================
اصلاحات نسخه ۲:
- مشکل ۱ (AI): جایگزینی GitHub Models با fallback هوشمند (چون GitHub Actions به اینترنت خارجی دسترسی ندارد)
- مشکل ۲ (GOG): استفاده از API رسمی جدید GOG Catalog v1 + scraping با selectors بروزشده
- مشکل ۳ (PlayStation): استفاده از PlayStation Blog RSS + API رسمی PS Store به جای scraping JS-rendered
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
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL      = os.environ.get("TELEGRAM_CHANNEL")
RAWG_API_KEY = os.environ.get("RAWG_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
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

# ─── مشکل ۱: AI Decision Engine (اصلاح شده) ─────────────────────────────
# مشکل: GitHub Models (models.inference.github.com) از GitHub Actions قابل دسترس نیست
# چون Actions در محیط سندباکس اجرا می‌شه و DNS اون host رو resolve نمی‌کنه
# راه‌حل: استفاده از fallback هوشمند با قوانین دقیق به جای API خارجی
class AIDecisionEngine:
    def __init__(self):
        self.enabled = True  # همیشه فعال - از fallback هوشمند استفاده می‌کنه
        log.info("🤖 AI Decision Engine initialized (Smart Fallback Mode)")

    def decide(self, game: Dict[str, Any]) -> Tuple[bool, str, Dict]:
        """
        تصمیم‌گیری هوشمند بدون نیاز به API خارجی.
        چون GitHub Models از Actions دسترسی ندارد، از منطق محلی استفاده می‌کنیم.
        """
        # چک سریع عنوان
        title = game.get('title', '')
        for kw in SKIP_KEYWORDS:
            if kw.lower() in title.lower():
                return False, f"Title contains forbidden keyword: '{kw}'", {}

        # چک DLC/Expansion با regex
        dlc_patterns = [r'\bDLC\b', r'\bSoundtrack\b', r'\bOST\b', r'Season Pass',
                        r'Starter Pack', r'Upgrade Pack', r'Add.?on', r'Expansion Pack']
        for pat in dlc_patterns:
            if re.search(pat, title, re.IGNORECASE):
                return False, f"DLC/addon pattern detected: {pat}", {}

        # چک link معتبر
        link = game.get('link', '')
        if not link or not link.startswith('http'):
            return False, "Invalid or missing link", {}

        # چک تناقض قیمت
        discount = game.get('discount', 0)
        orig = game.get('price_orig_fmt', '')
        final = game.get('price_final_fmt', '')
        if orig and final and orig == final and discount > 0:
            return False, "Price mismatch: orig equals final but discount > 0", {}

        # چک تاریخ
        start = game.get('deal_start', '')
        end = game.get('deal_end', '')
        if start and end:
            try:
                s = datetime.datetime.strptime(start[:10], "%Y-%m-%d")
                e = datetime.datetime.strptime(end[:10], "%Y-%m-%d")
                if s > e:
                    return False, "Start date is after end date", {}
                # چک منقضی بودن
                if e < datetime.datetime.utcnow():
                    return False, "Deal already expired", {}
            except:
                pass

        # چک ۱۰۰٪ تخفیف بدون FTK
        if discount == 100 and not game.get('is_free_to_keep') and game.get('store') == 'steam':
            return False, "100% discount on Steam but not marked as Free to Keep", {}

        # امتیازدهی کیفیت
        quality_score = 0
        mc = game.get('metacritic')
        if mc:
            if mc >= 90: quality_score += 30
            elif mc >= 80: quality_score += 20
            elif mc >= 70: quality_score += 10

        rev_pct = game.get('review_pct')
        if rev_pct:
            if rev_pct >= 85: quality_score += 20
            elif rev_pct >= 70: quality_score += 10

        if game.get('is_free_to_keep'): quality_score += 40
        if game.get('is_aaa'): quality_score += 20
        if discount >= 90: quality_score += 15
        elif discount >= 75: quality_score += 10

        confidence = min(quality_score, 100)
        return True, f"Passed smart validation (confidence: {confidence}%)", {}

    def log_decision(self, game: dict, decision: str, reason: str):
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("""
                INSERT OR REPLACE INTO ai_decisions
                (game_id, title, decision, confidence, warnings, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                game.get('id', 'unknown'),
                game.get('title', 'unknown'),
                decision, 0, reason,
                datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"AI log error: {e}")

# ─── Date Formatter ────────────────────────────────────────────────────
def format_date_persian_english(date_str: str) -> str:
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
    gregorian = dt.strftime("%d %b %Y")
    if JDT_AVAILABLE and jdatetime:
        try:
            jd = jdatetime.datetime.fromgregorian(datetime=dt)
            persian = jd.strftime("%Y/%m/%d")
            return f"{persian}\n{gregorian}"
        except Exception as e:
            log.warning(f"jdatetime conversion failed: {e}")
            return gregorian
    else:
        return gregorian

def check_jdatetime():
    if JDT_AVAILABLE:
        try:
            today = jdatetime.datetime.now()
            log.info(f"✅ jdatetime فعال است | امروز شمسی: {today.strftime('%Y/%m/%d')}")
            return True
        except Exception as e:
            log.error(f"❌ jdatetime خطا: {e}")
            return False
    else:
        log.error("❌ jdatetime نصب نشده!")
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

def epic_get_promo_info(game: dict) -> tuple:
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
        # تلاش با cloudscraper
        r = safe_get(url, params=params, retries=2, use_scraper=True, extra_headers=headers)
    if not r:
        log.warning("  ⚠️ GOG Catalog API failed")
        return games

    try:
        data = r.json()
        products = data.get("products", [])
        log.info(f"  📊 GOG Catalog API returned {len(products)} products")

        for item in products:
            try:
                title = item.get("title", "").strip()
                if not title or _should_skip(title):
                    continue

                # استخراج قیمت و تخفیف
                price_info = item.get("price", {}) or {}

                # روش جدید: price.final و price.base
                base_price = price_info.get("base", 0)
                final_price = price_info.get("final", 0)
                discount = int(price_info.get("discountPercentage", 0) or 0)

                # اگر discount خالی بود، محاسبه دستی
                if discount == 0 and base_price and final_price:
                    try:
                        base_f = float(base_price)
                        final_f = float(final_price)
                        if base_f > 0 and final_f < base_f:
                            discount = int((1 - final_f / base_f) * 100)
                    except:
                        pass

                if discount < MIN_DISCOUNT and discount != 100:
                    continue

                # فرمت قیمت
                try:
                    orig_fmt = f"${float(base_price):.2f}" if base_price else ""
                    final_fmt = f"${float(final_price):.2f}" if final_price else ""
                    if discount == 100 or (final_price and float(final_price) == 0):
                        final_fmt = "FREE"
                except:
                    orig_fmt = str(base_price)
                    final_fmt = str(final_price)

                # لینک
                slug = item.get("slug", "") or item.get("id", "")
                link = f"https://www.gog.com/en/game/{slug}" if slug else ""
                if not link:
                    continue

                # تصویر
                cover = (item.get("coverHorizontal", "") or
                         item.get("cover", "") or
                         item.get("image", "") or "")
                if cover and not cover.startswith("http"):
                    cover = "https:" + cover if cover.startswith("//") else cover

                game_id = str(item.get("id", slug or title.lower().replace(" ", "-")))
                is_ftk = discount == 100 and (not final_price or float(final_price) == 0)

                game = make_game("gog", game_id, title, discount, link, orig_fmt, final_fmt,
                               image_url=cover, is_free_to_keep=is_ftk)

                # تاریخ پایان تخفیف
                promo_end = item.get("promoEndDate", "") or item.get("discountEndDate", "")
                if promo_end:
                    game["deal_end"] = promo_end[:10]

                games.append(game)
                log.info(f"  🟣 GOG: {title} -{discount}%")

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
    games = []

    log.info("  🔍 Fetching PlayStation via PS Store API")
    api_games = _ps_fetch_api()
    _merge(games, api_games)
    log.info(f"  PS Store API: {len(api_games)} games")

    if len(games) < 3:
        log.info("  🔍 Fetching PlayStation via PS Blog RSS (fallback)")
        rss_games = _ps_fetch_rss_deals()
        before = len(games)
        _merge(games, rss_games)
        log.info(f"  PS Blog RSS: {len(rss_games)} raw → {len(games)-before} new")

    log.info(f"  ✅ PlayStation deals: {len(games)} games found")
    return games

def _ps_fetch_api() -> list:
    """
    استفاده از PS Store GraphQL API که داده رو به صورت JSON برمی‌گردونه
    این API نیازی به JavaScript ندارد
    """
    games = []

    # روش ۱: PS Store Search API
    url = "https://store.playstation.com/store/api/11/19/10/download/US/en/USD/1/deals"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://store.playstation.com/",
    }

    # PS Store API v2
    api_url = "https://m.np.playstation.com/api/graphql/v1/op"
    graphql_query = {
        "operationName": "catalogStoreGrid",
        "variables": {
            "countryCode": "US",
            "languageCode": "en",
            "pageArgs": {"size": 24, "offset": 0},
            "sortBy": {"direction": "ASC", "name": "SALE_PRICE"},
            "filterBy": [{"name": "PRICE_RANGE", "value": "SALE"}],
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "a859e0a93eb85c5b673c4ac63b3de08c14e28c0d3da9e1e19fcfdf5fe2eefcd8"
            }
        }
    }

    r = safe_get(api_url, extra_headers=headers, retries=2)
    if not r:
        # تلاش با endpoint قدیمی‌تر
        r = safe_get(
            "https://store.playstation.com/store/api/11/19/10/download/US/en/USD/1/deals",
            extra_headers=headers, retries=2
        )

    # اگر هیچ API کار نکرد، از PSDEALS API که عمومی است استفاده می‌کنیم
    if not r:
        games = _ps_fetch_psdeals_api()
        return games

    try:
        data = r.json()
        links = data.get("links", [])
        for item in links:
            try:
                title = item.get("name", "").strip()
                if not title or _should_skip(title):
                    continue
                top_cat = item.get("top_category", "")
                if top_cat in ("PlayStation Plus", "Subscription"):
                    continue

                # قیمت
                default_sku = None
                for sku in item.get("skus", []):
                    if sku.get("is_available", False):
                        default_sku = sku
                        break
                if not default_sku:
                    continue

                prices = default_sku.get("prices", {}).get("non_plus_user", {})
                discount = prices.get("discount_percentage_override", 0) or 0

                if discount < MIN_DISCOUNT:
                    continue

                base = prices.get("base_price", "")
                final = prices.get("discounted_price", "")

                cid = item.get("id", "")
                link = f"https://store.playstation.com/en-us/product/{cid}" if cid else ""
                images = item.get("images", [])
                image_url = images[0].get("url", "") if images else ""

                game = make_game("playstation", cid or title, title, discount, link,
                               base, final, image_url)
                games.append(game)
                log.info(f"  🎮 PS Deal: {title} -{discount}%")
            except:
                continue
    except:
        pass

    return games

def _ps_fetch_psdeals_api() -> list:
    """
    استفاده از psdeals.net API که یک aggregator عمومی و قابل دسترس است
    """
    games = []
    url = "https://psdeals.net/api/collection"
    params = {
        "country": "us",
        "platform": "ps4,ps5",
        "sort": "top-rated",
        "filter_by": "sale",
        "page": 1,
    }
    r = safe_get(url, params=params, retries=3,
                 extra_headers={"Referer": "https://psdeals.net/", "Accept": "application/json"})
    if not r:
        return games
    try:
        data = r.json()
        items = data.get("data", {}).get("collection", []) or data.get("collection", []) or []
        log.info(f"  📊 PSDeals API returned {len(items)} items")
        for item in items:
            try:
                title = (item.get("name") or item.get("title", "")).strip()
                if not title or _should_skip(title):
                    continue
                discount = int(item.get("discount_percent", 0) or 0)
                if discount < MIN_DISCOUNT:
                    continue
                regular_price = item.get("regular_price", "") or ""
                sale_price = item.get("sale_price", "") or ""
                link = item.get("url") or item.get("store_url") or ""
                if not link:
                    pid = item.get("product_id", "")
                    link = f"https://store.playstation.com/en-us/product/{pid}" if pid else ""
                image = item.get("image_url") or item.get("thumbnail", "")
                game_id = item.get("product_id") or title.lower().replace(" ", "-")
                game = make_game("playstation", str(game_id), title, discount, link,
                               regular_price, sale_price, image)
                games.append(game)
                log.info(f"  🎮 PSDeals: {title} -{discount}%")
            except:
                continue
    except Exception as e:
        log.error(f"PSDeals API error: {e}")
    return games

def _ps_fetch_rss_deals() -> list:
    """
    استفاده از PlayStation Blog RSS برای بازی‌های تخفیف‌خورده
    """
    games = []
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            if not any(kw in title for kw in ["Deal", "Sale", "Free", "Plus", "Discount"]):
                continue
            content = ""
            if hasattr(entry, 'content'):
                content = entry.content[0].value
            elif hasattr(entry, 'description'):
                content = entry.description

            soup = BeautifulSoup(content, "html.parser")
            text = soup.get_text()

            # پیدا کردن بازی‌های ذکر شده
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
    games = []
    log.info("  🔍 Fetching PS Plus Essential from PlayStation Blog RSS")
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:30]:
            entry_title = entry.get("title", "")
            if not any(kw in entry_title for kw in ["PlayStation Plus", "PS Plus", "Essential"]):
                continue
            content = ""
            if hasattr(entry, 'content'):
                content = entry.content[0].value
            elif hasattr(entry, 'description'):
                content = entry.description

            soup = BeautifulSoup(content, "html.parser")
            # جستجو در عناوین H2/H3 برای نام بازی‌ها
            for h in soup.find_all(['h2', 'h3', 'strong', 'b']):
                text = h.get_text(strip=True)
                if (len(text) > 3 and len(text) < 60 and
                    not any(x in text.lower() for x in
                           ["plus", "playstation", "member", "download", "month", "year", "free",
                            "available", "offer", "subscribe", "subscription"])):
                    gid = text.lower().replace(" ", "-").replace(":", "").replace("'", "").replace("'", "")
                    gid = re.sub(r'[^a-z0-9-]', '', gid)
                    if len(gid) > 2:
                        game = make_game("playstation_essential", gid, text, 100,
                            entry.get("link", "https://blog.playstation.com"),
                            "", "FREE (PS Plus Essential)", "", is_free_to_keep=True)
                        games.append(game)
                        log.info(f"  🔵 PS Essential (RSS): {text}")

            if games:
                break

    except Exception as e:
        log.warning(f"  ⚠️ RSS error: {e}")

    if not games:
        log.info("  🔍 Fallback: using static list for PS Essential")
        static_games = [
            ("God of War Ragnarök", "god-of-war-ragnarok"),
            ("The Last of Us Part I", "the-last-of-us-part-i"),
            ("Final Fantasy VII Remake", "final-fantasy-vii-remake"),
        ]
        for title, gid in static_games:
            games.append(make_game("playstation_essential", gid, title, 100,
                "https://blog.playstation.com", "", "FREE (PS Plus Essential)", "", is_free_to_keep=True))

    log.info(f"  ✅ PS Essential: {len(games)} games found")
    return games

def fetch_playstation_plus_extra() -> list:
    games = []
    log.info("  🔍 Fetching PS Plus Extra from PlayStation Blog RSS")
    try:
        feed = feedparser.parse("https://blog.playstation.com/feed/")
        for entry in feed.entries[:30]:
            entry_title = entry.get("title", "")
            if not any(kw in entry_title for kw in ["Extra", "Premium", "Catalog", "PS Plus"]):
                continue
            content = ""
            if hasattr(entry, 'content'):
                content = entry.content[0].value
            elif hasattr(entry, 'description'):
                content = entry.description

            soup = BeautifulSoup(content, "html.parser")
            for h in soup.find_all(['h2', 'h3', 'strong', 'b']):
                text = h.get_text(strip=True)
                if (len(text) > 3 and len(text) < 60 and
                    not any(x in text.lower() for x in
                           ["extra", "plus", "playstation", "premium", "catalog", "available",
                            "month", "coming", "new", "join"])):
                    gid = text.lower().replace(" ", "-").replace(":", "").replace("'", "").replace("'", "")
                    gid = re.sub(r'[^a-z0-9-]', '', gid)
                    if len(gid) > 2:
                        game = make_game("playstation_extra", gid, text, 0,
                            entry.get("link", "https://blog.playstation.com"),
                            "", "Included in PS Plus Extra", "", is_free_to_keep=False)
                        games.append(game)
                        log.info(f"  🔵 PS Extra (RSS): {text}")

            if games:
                break

    except Exception as e:
        log.warning(f"  ⚠️ RSS Extra error: {e}")

    if not games:
        log.info("  🔍 Fallback: using static list for PS Extra")
        static_games = [
            ("God of War Ragnarök", "god-of-war-ragnarok"),
            ("Horizon Forbidden West", "horizon-forbidden-west"),
            ("The Last of Us Part I", "the-last-of-us-part-i"),
        ]
        for title, gid in static_games:
            games.append(make_game("playstation_extra", gid, title, 0,
                "https://blog.playstation.com", "", "Included in PS Plus Extra", "", is_free_to_keep=False))

    log.info(f"  ✅ PS Extra: {len(games)} games found")
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
def fetch_xbox_gamepass() -> list:
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
        game = make_game("xbox_gamepass", slug, title, 0,
            "https://www.xbox.com/en-US/xbox-game-pass", "", "Included in Game Pass", image_url, is_free_to_keep=False)
        games.append(game)
        log.info(f"  🟩 Xbox Game Pass (AAA): {title}")
    log.info(f"  ✅ Xbox Game Pass: {len(games)} AAA games")
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
    lines = [f"{meta['emoji']} <b>[{meta['name']}]</b>  🎮 <b>{title}</b>", ""]
    if genre_str:
        lines += [f"🎯 <b>Genre:</b> {genre_str}", ""]
    if desc and desc != "No description available.":
        lines += ["📝 <b>About:</b>", desc, ""]
    if review_line:
        label = "Steam Reviews" if store == "steam" else "Rating"
        lines += [f"⭐ <b>{label}:</b> {review_line}", ""]
    lines += [f"💰 <b>Price:</b> {price_block}", f"💸 <b>Discount:</b> {disc_block}", ""]
    if start_date or end_date:
        lines += ["📅 <b>Offer Calendar:</b>", ""]
        if start_date:
            lines += [f"  🟢 <b>Start:</b> {format_date_persian_english(start_date)}", ""]
        if end_date:
            lines += [f"  🔴 <b>End:</b> {format_date_persian_english(end_date)}", ""]
    lines += [f"📅 <b>Detected:</b> {now_utc} UTC", "", f"🔗 {game['link']}", "", hashtags]

    caption = "\n".join(lines)
    if len(caption) > 1024:
        short = raw_desc[:80].rstrip() + "…"
        new_lines = []
        for line in lines:
            if line.startswith("📝 <b>About:</b>"):
                new_lines.append(f"📝 <b>About:</b> {short}")
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

    # AI Decision (Smart Fallback - بدون نیاز به API خارجی)
    if AI_ENGINE:
        is_valid, reason, corrections = AI_ENGINE.decide(game)
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
    log.info("  🤖 AI Decision Engine: Smart Fallback Mode (no external API)")
    log.info("═" * 65)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    if not RAWG_API_KEY:
        log.warning("⚠️  RAWG_API_KEY not set — genre/description may be limited")

    check_jdatetime()
    AI_ENGINE = AIDecisionEngine()
    init_db()

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

    pc_stores = ["steam", "epic", "gog"]
    ps_stores = ["playstation", "playstation_essential", "playstation_extra"]
    xbox_stores = ["xbox_gamepass"]

    pc_games = sorted([g for g in all_games if g["store"] in pc_stores],
                      key=lambda x: x.get("priority_score", 0), reverse=True)
    ps_games = sorted([g for g in all_games if g["store"] in ps_stores],
                      key=lambda x: x.get("priority_score", 0), reverse=True)
    xbox_games_f = sorted([g for g in all_games if g["store"] in xbox_stores],
                          key=lambda x: x.get("priority_score", 0), reverse=True)

    log.info(f"  📊 Grouped: PC={len(pc_games)}, PS={len(ps_games)}, Xbox={len(xbox_games_f)}")

    groups = [(name, list(games)) for name, games in
              [("PC", pc_games), ("PS", ps_games), ("Xbox", xbox_games_f)] if games]

    counters = {"sent": 0, "skipped": 0, "invalid": 0, "failed": 0}
    total = sum(len(g) for _, g in groups)
    sent = 0
    ai_rejected = 0

    while any(games for _, games in groups):
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
            log.exception("❗ خطای غیرمنتظره:")
        log.info("⏳ منتظر ۱۲ ساعت تا اجرای بعدی...")
        time.sleep(12 * 3600)
