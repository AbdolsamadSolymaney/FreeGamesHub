"""
FreeGamesHub — Steam + Epic Games Store + GOG + PlayStation + Xbox Game Pass
================================================================================
نسخه نهایی با ارسال چرخشی (PC → PS → Xbox)
- دریافت خودکار تخفیف‌ها و بازی‌های رایگان از فروشگاه‌های معتبر
- تشخیص بازی‌های AAA با Metacritic (≥75) و RAWG Rating (≥80%)
- تکمیل اطلاعات از RAWG و Steam
- کش ۲۴ ساعته برای جلوگیری از ارسال تکراری
- حداقل تخفیف: ۷۵٪
- ارسال چرخشی: PC → PS → Xbox → PC → PS → Xbox → ...
- اجرای خودکار هر ۱۲ ساعت
"""

import os
import time
import logging
import hashlib
import requests
import sqlite3
import datetime
import re
from bs4 import BeautifulSoup
import cloudscraper
import feedparser

# ═══════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL      = os.environ.get("TELEGRAM_CHANNEL")
RAWG_API_KEY = os.environ.get("RAWG_API_KEY", "")
DB_FILE      = "games.db"
MIN_DISCOUNT = 75

# آستانه تشخیص AAA با Metacritic
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

# ═══════════════════════════════════════════════════
#  CACHE ۲۴ ساعته
# ═══════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════
#  HTTP HELPER
# ═══════════════════════════════════════════════════
def safe_get(url, params=None, retries=3, delay=2, use_scraper=False, extra_headers=None):
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

# ═══════════════════════════════════════════════════
#  RAWG API
# ═══════════════════════════════════════════════════
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

    r = safe_get(
        "https://api.rawg.io/api/games",
        params=params,
        extra_headers={"Referer": "https://rawg.io/"},
        retries=2,
        delay=1
    )
    if not r:
        _rawg_cache[cache_key] = None
        return None

    try:
        results = r.json().get("results", [])
        if not results:
            _rawg_cache[cache_key] = None
            return None

        best       = None
        best_score = 0
        title_low  = clean.lower()
        for item in results:
            name = item.get("name", "").lower()
            if name == title_low:
                score = 100
            elif title_low in name or name in title_low:
                score = 80
            else:
                t_words = set(title_low.split())
                i_words = set(name.split())
                score   = len(t_words & i_words) / max(len(t_words), 1) * 60
            if score > best_score:
                best_score = score
                best = item

        if best_score < 40 or not best:
            _rawg_cache[cache_key] = None
            return None

        description = ""
        detail_params = {"key": RAWG_API_KEY}
        dr = safe_get(
            f"https://api.rawg.io/api/games/{best['id']}",
            params=detail_params,
            extra_headers={"Referer": "https://rawg.io/"},
            retries=2,
            delay=1
        )
        if dr:
            raw_desc = dr.json().get("description", "") or dr.json().get("description_raw", "")
            if raw_desc:
                description = BeautifulSoup(raw_desc, "html.parser").get_text().strip()
                description = re.sub(r'\n{3,}', '\n\n', description)

        genres       = [g["name"] for g in best.get("genres", [])]
        rawg_rating  = best.get("rating", 0)
        rating_count = best.get("ratings_count", 0)
        rating_pct   = round(rawg_rating / 5 * 100) if rawg_rating else None
        metacritic   = best.get("metacritic")
        bg_image     = best.get("background_image", "") or ""

        result = {
            "genres":           genres,
            "description":      description,
            "rating_pct":       rating_pct,
            "ratings_count":    rating_count,
            "metacritic":       metacritic,
            "rawg_rating":      rawg_rating,
            "background_image": bg_image,
        }
        _rawg_cache[cache_key] = result
        return result

    except Exception as e:
        log.error(f"RAWG error for '{title}': {e}")
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
        game["review_pct"]   = rawg["rating_pct"]
        game["review_count"] = rawg["ratings_count"]
        game["review_desc"]  = f"RAWG {rawg['rawg_rating']:.1f}/5"
    if rawg.get("metacritic"):
        game["metacritic"] = rawg["metacritic"]
    if rawg.get("background_image"):
        game["rawg_image"] = rawg["background_image"]

# ═══════════════════════════════════════════════════
#  GAME OBJECT FACTORY
# ═══════════════════════════════════════════════════
def _should_skip(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in SKIP_KEYWORDS)

def make_game(store: str, game_id: str, title: str, discount: int,
              link: str, orig_fmt: str = "", final_fmt: str = "",
              image_url: str = "", is_free_to_keep: bool = False,
              description: str = "", genres: list = None,
              review_pct: int = None, review_count: int = None,
              review_desc: str = "") -> dict:
    return {
        "store":            store,
        "id":               str(game_id),
        "title":            title,
        "link":             link,
        "discount":         discount,
        "price_orig_fmt":   orig_fmt,
        "price_final_fmt":  final_fmt,
        "image_url":        image_url,
        "is_free_to_keep":  is_free_to_keep,
        "description":      description,
        "genres":           genres or [],
        "review_pct":       review_pct,
        "review_count":     review_count,
        "review_desc":      review_desc,
        "metacritic":       None,
        "rawg_image":       "",
        "steam_image":      "",
        "deal_start":       "",
        "deal_end":         "",
    }

def _merge(base: list, new_items: list):
    seen = {(g["store"], g["id"]) for g in base}
    for g in new_items:
        key = (g["store"], g["id"])
        if key not in seen:
            base.append(g)
            seen.add(key)

# ═══════════════════════════════════════════════════
#  IMAGE URL
# ═══════════════════════════════════════════════════
def get_image_candidates(game: dict) -> list[str]:
    store = game["store"]
    gid   = game["id"]
    candidates = []

    if game.get("steam_image"):
        candidates.append(game["steam_image"])
    
    if store == "steam":
        candidates.extend([
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{gid}/capsule_616x353.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{gid}/header.jpg",
        ])
    
    if game.get("rawg_image"):
        candidates.append(game["rawg_image"])
    
    if game.get("image_url"):
        img = game["image_url"]
        img = img.replace("_small", "_large").replace("_thumb", "_original")
        img = re.sub(r'/\d+x\d+/', '/original/', img)
        candidates.append(img)
        candidates.append(game["image_url"])
    
    return candidates

# ═══════════════════════════════════════════════════
#  STEAM SOURCES
# ═══════════════════════════════════════════════════
def _steam_fetch_featured() -> list[dict]:
    games = []
    r = safe_get(
        "https://store.steampowered.com/api/featuredcategories/",
        params={"cc": "US", "l": "english"},
    )
    if not r:
        return games
    try:
        data = r.json()
        for item in data.get("specials", {}).get("items", []):
            name = item.get("name", "")
            if not name or _should_skip(name):
                continue
            appid    = str(item.get("id", ""))
            discount = item.get("discount_percent", 0)
            orig     = item.get("original_price", 0)
            final    = item.get("final_price", 0)
            if not appid:
                continue
            games.append(make_game(
                "steam", appid, name, discount,
                f"https://store.steampowered.com/app/{appid}/",
                f"${orig/100:.2f}" if orig else "",
                f"${final/100:.2f}" if final else "",
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
            ))
        for section in ["top_sellers", "new_releases"]:
            for item in data.get(section, {}).get("items", []):
                disc = item.get("discount_percent", 0)
                if disc < MIN_DISCOUNT and disc != 100:
                    continue
                name  = item.get("name", "")
                appid = str(item.get("id", ""))
                if not appid or not name or _should_skip(name):
                    continue
                orig  = item.get("original_price", 0)
                final = item.get("final_price", 0)
                games.append(make_game(
                    "steam", appid, name, disc,
                    f"https://store.steampowered.com/app/{appid}/",
                    f"${orig/100:.2f}" if orig else "",
                    f"${final/100:.2f}" if final else "",
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                ))
    except Exception as e:
        log.error(f"Steam Featured parse error: {e}")
    return games

def _steam_fetch_html_search() -> list[dict]:
    games = []
    r = safe_get(
        "https://store.steampowered.com/search/",
        params={"specials": 1, "cc": "US", "l": "english"},
    )
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
                if _should_skip(title):
                    continue
                disc_el  = row.select_one(".discount_pct")
                discount = 0
                if disc_el:
                    try:
                        discount = int(disc_el.text.strip().replace("-", "").replace("%", ""))
                    except ValueError:
                        pass
                if discount < MIN_DISCOUNT and discount != 100:
                    continue
                orig_el  = row.select_one(".discount_original_price")
                final_el = row.select_one(".discount_final_price")
                games.append(make_game(
                    "steam", appid, title, discount,
                    f"https://store.steampowered.com/app/{appid}/",
                    orig_el.text.strip() if orig_el else "",
                    final_el.text.strip() if final_el else "",
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                ))
            except Exception as e:
                log.debug(f"Steam HTML row parse error: {e}")
    except Exception as e:
        log.error(f"Steam HTML search parse error: {e}")
    return games

def _steam_fetch_free_to_keep() -> list[dict]:
    games = []
    r = safe_get(
        "https://store.steampowered.com/search/results/",
        params={"specials": 1, "maxprice": "free", "cc": "US",
                "l": "english", "json": 1, "count": 50},
    )
    if r:
        try:
            data = r.json()
            for item in data.get("items", []):
                logo  = item.get("logo", "")
                match = re.search(r"/apps/(\d+)/", logo)
                if not match:
                    continue
                appid = match.group(1)
                title = BeautifulSoup(item.get("name", ""), "html.parser").get_text().strip()
                if not title or _should_skip(title):
                    continue
                price_str = str(item.get("price", "")).lower()
                if "free" not in price_str and price_str != "0":
                    continue
                games.append(make_game(
                    "steam", appid, title, 100,
                    f"https://store.steampowered.com/app/{appid}/",
                    final_fmt="FREE",
                    image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                    is_free_to_keep=True,
                ))
                log.info(f"  🎁 Steam FTK (JSON): {title}")
        except Exception as e:
            log.error(f"Steam FTK JSON error: {e}")

    if not games:
        r2 = safe_get(
            "https://store.steampowered.com/search/",
            params={"specials": 1, "maxprice": "free", "cc": "US", "l": "english"},
        )
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
                    if _should_skip(title):
                        continue
                    disc_el = row.select_one(".discount_pct")
                    if not disc_el or "-100%" not in disc_el.text:
                        continue
                    games.append(make_game(
                        "steam", appid, title, 100,
                        f"https://store.steampowered.com/app/{appid}/",
                        final_fmt="FREE",
                        image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
                        is_free_to_keep=True,
                    ))
                    log.info(f"  🎁 Steam FTK (HTML): {title}")
            except Exception as e:
                log.error(f"Steam FTK HTML error: {e}")

    log.info(f"Steam Free to Keep: {len(games)}")
    return games

def steam_get_details(appid: str) -> dict | None:
    time.sleep(1.2)
    r = safe_get(
        "https://store.steampowered.com/api/appdetails",
        params={"appids": appid, "cc": "us", "l": "english"},
    )
    if not r:
        return None
    try:
        data = r.json()
        app  = data.get(str(appid), {})
        if not app.get("success"):
            return None
        return app["data"]
    except Exception as e:
        log.error(f"Steam AppDetails error ({appid}): {e}")
        return None

def steam_get_reviews(appid: str):
    r = safe_get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
    )
    if not r:
        return None, None, ""
    try:
        qs    = r.json().get("query_summary", {})
        pos   = qs.get("total_positive", 0)
        total = qs.get("total_reviews", 0)
        desc  = qs.get("review_score_desc", "")
        if total == 0:
            return None, None, ""
        return round(pos / total * 100), total, desc
    except:
        return None, None, ""

def steam_get_promo_info(appid: str, is_ftk: bool) -> tuple[str, str]:
    r = safe_get(f"https://store.steampowered.com/app/{appid}/", retries=2, delay=1)
    if r:
        try:
            soup  = BeautifulSoup(r.text, "html.parser")
            block = soup.select_one(".discount_block")
            if block:
                text  = block.get_text(separator=" ", strip=True)
                match = re.search(
                    r"Offer ends\s+([\w]+\s+\d{1,2},\s+\d{4})",
                    text, re.IGNORECASE
                )
                if match:
                    end_str = match.group(1)
                    try:
                        end_dt  = datetime.datetime.strptime(end_str, "%b %d, %Y")
                        display = end_dt.strftime("%Y-%m-%d")
                        prefix  = "FTK" if is_ftk else "END"
                        return display, f"{prefix}:{display}"
                    except ValueError:
                        pass
        except Exception as e:
            log.debug(f"Steam date extraction failed ({appid}): {e}")

    week = current_week_anchor()
    prefix = "FTK:" if is_ftk else ""
    return "Unknown", f"{prefix}{week}"

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

# ═══════════════════════════════════════════════════
#  EPIC GAMES STORE
# ═══════════════════════════════════════════════════
EPIC_GQL_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

def fetch_epic_games() -> list[dict]:
    games = []
    r = safe_get(
        EPIC_GQL_URL,
        params={"locale": "en-US", "country": "US", "allowCountries": "US"},
        extra_headers={"Referer": "https://store.epicgames.com/"},
    )
    if not r:
        log.error("Epic API failed")
        return games

    try:
        data     = r.json()
        elements = (
            data.get("data", {})
                .get("Catalog", {})
                .get("searchStore", {})
                .get("elements", [])
        )
        now = datetime.datetime.utcnow()

        for el in elements:
            title = el.get("title", "").strip()
            if not title or _should_skip(title):
                continue
            promotions   = el.get("promotions") or {}
            promo_offers = promotions.get("promotionalOffers", [])
            if not promo_offers:
                continue

            active_offer = None
            for offer_group in promo_offers:
                for offer in offer_group.get("promotionalOffers", []):
                    start = offer.get("startDate", "")
                    end   = offer.get("endDate", "")
                    try:
                        start_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")).replace(tzinfo=None)
                        end_dt   = datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                        if start_dt <= now <= end_dt:
                            active_offer = {"start": start_dt, "end": end_dt}
                            break
                    except:
                        pass
                if active_offer:
                    break

            if not active_offer:
                continue

            price_info  = el.get("price", {}) or {}
            total_price = price_info.get("totalPrice", {}) or {}
            orig_cents  = total_price.get("originalPrice", 0)
            orig_fmt    = f"${orig_cents/100:.2f}" if orig_cents else ""

            image_url = ""
            for img_type in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail"):
                for img in el.get("keyImages", []):
                    if img.get("type") == img_type:
                        image_url = img.get("url", "")
                        break
                if image_url:
                    break

            slug = (
                el.get("catalogNs", {}).get("mappings", [{}])[0].get("pageSlug", "")
                or el.get("productSlug", "")
                or el.get("urlSlug", "")
            )
            link = f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games"

            game_id = el.get("id") or el.get("productSlug") or slug or title
            end_display = active_offer["end"].strftime("%Y-%m-%d")

            game = make_game(
                "epic", str(game_id), title, 100,
                link,
                orig_fmt=orig_fmt,
                final_fmt="FREE",
                image_url=image_url,
                is_free_to_keep=True,
            )
            game["deal_start"] = active_offer["start"].strftime("%Y-%m-%d")
            game["deal_end"]   = end_display
            games.append(game)
            log.info(f"  ⬛ Epic Free: {title} (ends {end_display})")

    except Exception as e:
        log.error(f"Epic parse error: {e}")

    log.info(f"Epic Games total: {len(games)}")
    return games

def epic_get_promo_info(game: dict) -> tuple[str, str]:
    r = safe_get(
        EPIC_GQL_URL,
        params={"locale": "en-US", "country": "US", "allowCountries": "US"},
        extra_headers={"Referer": "https://store.epicgames.com/"},
    )
    if not r:
        return "Unknown", f"FTK:{current_week_anchor()}"

    try:
        data     = r.json()
        elements = (
            data.get("data", {})
                .get("Catalog", {})
                .get("searchStore", {})
                .get("elements", [])
        )
        now = datetime.datetime.utcnow()
        for el in elements:
            eid = str(el.get("id") or el.get("productSlug") or "")
            if eid != game["id"] and el.get("title", "") != game["title"]:
                continue
            promotions   = el.get("promotions") or {}
            promo_offers = promotions.get("promotionalOffers", [])
            for offer_group in promo_offers:
                for offer in offer_group.get("promotionalOffers", []):
                    end = offer.get("endDate", "")
                    try:
                        end_dt = datetime.datetime.fromisoformat(end.replace("Z", "+00:00")).replace(tzinfo=None)
                        if end_dt > now:
                            display = end_dt.strftime("%Y-%m-%d")
                            return display, f"FTK:{display}"
                    except:
                        pass
    except:
        pass

    return "Unknown", f"FTK:{current_week_anchor()}"

# ═══════════════════════════════════════════════════
#  GOG — نسخه بهبودیافته با سلکتورهای جدید
# ═══════════════════════════════════════════════════

def fetch_gog_games() -> list[dict]:
    """گرفتن بازی‌های GOG با تخفیف ≥۷۵٪ و رایگان"""
    games = []
    
    # منبع ۱: API رسمی GOG (با پارامترهای جدید)
    log.info("  🔍 Fetching GOG via API")
    games_api = _gog_fetch_api()
    _merge(games, games_api)
    log.info(f"  GOG API: {len(games_api)} games")
    
    # منبع ۲: اسکرپینگ صفحه اصلی (برای بازی‌های رایگان)
    log.info("  🔍 Fetching GOG via scraping")
    games_scrape = _gog_fetch_scrape()
    before = len(games)
    _merge(games, games_scrape)
    log.info(f"  GOG Scrape: {len(games_scrape)} raw → {len(games)-before} new")
    
    # منبع ۳: صفحه تخفیف‌های ویژه
    log.info("  🔍 Fetching GOG via specials page")
    games_specials = _gog_fetch_specials()
    before = len(games)
    _merge(games, games_specials)
    log.info(f"  GOG Specials: {len(games_specials)} raw → {len(games)-before} new")
    
    log.info(f"  ✅ GOG total: {len(games)} games found")
    return games

def _gog_fetch_api() -> list[dict]:
    """گرفتن بازی‌ها از API رسمی GOG با پارامترهای به‌روز"""
    games = []
    
    # API جدید GOG
    url = "https://catalog.gog.com/v1/catalog"
    params = {
        "limit": 100,
        "order": "desc:trending",
        "discounted": "true",
        "productType": "in:game,dlc",
        "page": 1,
        "countryCode": "US",
        "currencyCode": "USD"
    }
    
    r = safe_get(url, params=params, use_scraper=True, retries=3, delay=2,
                 extra_headers={"Referer": "https://www.gog.com/"})
    
    if not r:
        log.warning("  ⚠️ GOG API failed")
        return games
    
    try:
        data = r.json()
        products = data.get("products", [])
        
        for item in products:
            title = item.get("title", "").strip()
            if not title or _should_skip(title):
                continue
            
            # استخراج قیمت و تخفیف
            price_info = item.get("price", {})
            if not price_info:
                continue
            
            discount = int(price_info.get("discountPercentage", 0) or 0)
            
            # فیلتر تخفیف ≥۷۵٪ یا رایگان
            if discount < MIN_DISCOUNT and discount != 100:
                continue
            
            # قیمت‌ها
            base_price = price_info.get("base", 0)
            final_price = price_info.get("final", 0)
            
            orig_fmt = f"${float(base_price):.2f}" if base_price else ""
            final_fmt = f"${float(final_price):.2f}" if final_price else ("FREE" if discount == 100 else "")
            
            # لینک
            slug = item.get("slug", "")
            link = f"https://www.gog.com/en/game/{slug}" if slug else "https://www.gog.com"
            
            # تصویر
            cover = item.get("coverHorizontal", "") or item.get("cover", "")
            
            game_id = str(item.get("id", slug or title))
            is_ftk = (discount == 100 and final_price == 0)
            
            game = make_game(
                "gog", game_id, title, discount,
                link, orig_fmt, final_fmt,
                image_url=cover,
                is_free_to_keep=is_ftk
            )
            games.append(game)
            log.info(f"  🟣 GOG API: {title} -{discount}%")
            
    except Exception as e:
        log.warning(f"  ⚠️ GOG API parse error: {e}")
    
    return games

def _gog_fetch_scrape() -> list[dict]:
    """اسکرپینگ صفحه GOG برای بازی‌های رایگان و تخفیفی"""
    games = []
    
    # صفحه اصلی GOG
    urls = [
        "https://www.gog.com/en/games",
        "https://www.gog.com/en/games?discounted=true",
        "https://www.gog.com/en/games?priceRange=0,0"
    ]
    
    for url in urls:
        r = safe_get(url, use_scraper=True, retries=3, delay=3,
                     extra_headers={"Referer": "https://www.gog.com/"})
        if r:
            break
    
    if not r:
        log.warning("  ⚠️ GOG scrape failed")
        return games
    
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        # سلکتورهای جدید GOG (بر اساس ساختار فعلی)
        products = (
            soup.select("[data-testid='productCard']") or
            soup.select(".product-tile") or
            soup.select(".product-card") or
            soup.select("[class*='product']") or
            soup.select("[data-product-id]")
        )
        
        # اگر هیچ محصولی پیدا نشد، از سلکتورهای عمومی استفاده کن
        if not products:
            products = soup.select("a[href*='/en/game/']")
            # فیلتر کردن لینک‌های تکراری
            seen = set()
            unique_products = []
            for p in products:
                href = p.get("href", "")
                if href and href not in seen:
                    seen.add(href)
                    unique_products.append(p)
            products = unique_products
        
        for prod in products[:50]:
            try:
                # استخراج عنوان
                title_el = (
                    prod.select_one("[data-testid='productTitle']") or
                    prod.select_one(".product-title") or
                    prod.select_one(".title") or
                    prod.select_one("h3, h4") or
                    prod.select_one("[class*='title']")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or _should_skip(title):
                    continue
                
                # استخراج تخفیف
                discount_el = (
                    prod.select_one("[data-testid='discountPercentage']") or
                    prod.select_one(".discount-percentage") or
                    prod.select_one("[class*='discount']")
                )
                discount = 0
                if discount_el:
                    disc_text = discount_el.get_text(strip=True).replace("%", "").replace("-", "")
                    try:
                        discount = int(disc_text)
                    except:
                        pass
                
                # اگر تخفیف پیدا نشد، از قیمت‌ها استفاده کن
                if discount == 0:
                    price_el = prod.select_one(".final-price, .price, [class*='price']")
                    if price_el:
                        price_text = price_el.get_text(strip=True).lower()
                        if "free" in price_text:
                            discount = 100
                
                if discount < MIN_DISCOUNT and discount != 100:
                    continue
                
                # استخراج قیمت‌ها
                orig_el = prod.select_one(".original-price, .old-price, [class*='original']")
                final_el = prod.select_one(".final-price, .current-price, [class*='final']")
                orig = orig_el.get_text(strip=True) if orig_el else ""
                final = final_el.get_text(strip=True) if final_el else ""
                
                # استخراج لینک
                link_el = prod.select_one("a[href*='/en/game/']")
                if not link_el:
                    link_el = prod
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.gog.com" + link
                if not link or "/en/game/" not in link:
                    continue
                
                # استخراج تصویر
                img_el = prod.select_one("img")
                image_url = img_el.get("src") or img_el.get("data-src", "") if img_el else ""
                if image_url and image_url.startswith("//"):
                    image_url = "https:" + image_url
                if image_url:
                    image_url = image_url.replace("_small", "_large").replace("_thumb", "_original")
                
                # استخراج ID بازی
                game_id = ""
                if link:
                    match = re.search(r"/game/([^/?]+)", link)
                    if match:
                        game_id = match.group(1)
                if not game_id:
                    game_id = title.lower().replace(" ", "-").replace(":", "")
                
                is_ftk = (discount == 100)
                
                game = make_game(
                    "gog", game_id, title, discount,
                    link, orig, final, image_url,
                    is_free_to_keep=is_ftk
                )
                games.append(game)
                log.info(f"  🟣 GOG Scrape: {title} -{discount}%")
                
            except Exception as e:
                continue
                
    except Exception as e:
        log.warning(f"  ⚠️ GOG scrape parse error: {e}")
    
    return games

def _gog_fetch_specials() -> list[dict]:
    """گرفتن بازی‌ها از صفحه تخفیف‌های ویژه GOG"""
    games = []
    
    url = "https://www.gog.com/en/games?discounted=true&page=1"
    r = safe_get(url, use_scraper=True, retries=3, delay=3,
                 extra_headers={"Referer": "https://www.gog.com/"})
    
    if not r:
        return games
    
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        # پیدا کردن تمام کارت‌های بازی
        cards = (
            soup.select("[data-testid='productCard']") or
            soup.select(".product-tile") or
            soup.select("[class*='product']")
        )
        
        for card in cards[:30]:
            try:
                # عنوان
                title_el = card.select_one("[data-testid='productTitle'], .title, h3")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or _should_skip(title):
                    continue
                
                # تخفیف
                disc_el = card.select_one("[class*='discount']")
                discount = 0
                if disc_el:
                    disc_text = disc_el.get_text(strip=True).replace("%", "").replace("-", "")
                    try:
                        discount = int(disc_text)
                    except:
                        pass
                
                if discount < MIN_DISCOUNT and discount != 100:
                    continue
                
                # لینک
                link_el = card.select_one("a[href*='/en/game/']")
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.gog.com" + link
                
                # تصویر
                img_el = card.select_one("img")
                image_url = img_el.get("src") or img_el.get("data-src", "") if img_el else ""
                if image_url and image_url.startswith("//"):
                    image_url = "https:" + image_url
                
                game_id = ""
                if link:
                    match = re.search(r"/game/([^/?]+)", link)
                    if match:
                        game_id = match.group(1)
                if not game_id:
                    game_id = title.lower().replace(" ", "-")
                
                is_ftk = (discount == 100)
                
                game = make_game(
                    "gog", game_id, title, discount,
                    link, "", "", image_url,
                    is_free_to_keep=is_ftk
                )
                games.append(game)
                
            except Exception as e:
                continue
                
    except Exception as e:
        log.warning(f"  ⚠️ GOG specials parse error: {e}")
    
    return games

def gog_get_promo_info(game: dict) -> tuple[str, str]:
    r = safe_get(game["link"], retries=2, delay=1,
                 extra_headers={"Referer": "https://www.gog.com/"})
    if r:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select("[data-test-id='discountCountdown'], .discount-countdown, [class*='countdown']"):
                text = el.get_text(strip=True)
                match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                if match:
                    display = match.group(1)
                    prefix  = "FTK" if game.get("is_free_to_keep") else "END"
                    return display, f"{prefix}:{display}"
        except Exception as e:
            log.debug(f"GOG date extraction failed: {e}")

    week   = current_week_anchor()
    prefix = "FTK:" if game.get("is_free_to_keep") else ""
    return "Unknown", f"{prefix}{week}"

# ═══════════════════════════════════════════════════
#  PLAYSTATION DEALS
# ═══════════════════════════════════════════════════
def fetch_playstation_deals() -> list[dict]:
    """بازی‌های با تخفیف ≥۷۵٪ از فروشگاه PlayStation"""
    games = []
    
    urls = [
        "https://store.playstation.com/en-us/pages/deals",
        "https://store.playstation.com/en-us/deals",
        "https://store.playstation.com/en-us/deals?direction=desc&sort=release_date"
    ]
    
    r = None
    for url in urls:
        log.info(f"  🔍 Trying PlayStation Deals from {url}")
        r = safe_get(url, use_scraper=True, retries=3, delay=3,
                     extra_headers={"Referer": "https://store.playstation.com/"})
        if r:
            break
    
    if not r:
        log.error("  ❌ All PlayStation deals pages failed")
        return games

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        products = []
        for a in soup.select("a[href*='/en-us/product/']"):
            parent = a.parent
            if parent:
                products.append(parent)
        
        if not products:
            products = soup.select("[data-qa*='product']")
        if not products:
            products = soup.select(".product-card, .product-tile, [class*='product']")
        if not products:
            products = soup.select("li[data-product-id]")
        if not products:
            products = soup.select("[class*='game'], [class*='offer']")
        
        log.info(f"  🔍 Found {len(products)} potential products")
        
        for prod in products[:50]:
            try:
                title_el = (
                    prod.select_one("h3") or
                    prod.select_one("[data-qa*='title']") or
                    prod.select_one(".title") or
                    prod.select_one(".game-title") or
                    prod.select_one("[class*='title']")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or _should_skip(title):
                    continue
                
                discount_el = (
                    prod.select_one("[class*='discount']") or
                    prod.select_one(".price__discount") or
                    prod.select_one("[data-qa*='discount']")
                )
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
                image_url = ""
                if img_el:
                    image_url = img_el.get("src") or img_el.get("data-src", "")
                    if image_url.startswith("//"):
                        image_url = "https:" + image_url
                    image_url = image_url.replace("_small", "_large").replace("_thumb", "_original")
                    image_url = re.sub(r'/\d+x\d+/', '/original/', image_url)
                
                game_id = ""
                if link:
                    match = re.search(r"/product/([^/?]+)", link)
                    if match:
                        game_id = match.group(1)
                if not game_id:
                    game_id = title.lower().replace(" ", "-").replace(":", "")
                
                orig_el = prod.select_one(".price__old, .original-price")
                final_el = prod.select_one(".price__current, .final-price")
                orig = orig_el.get_text(strip=True) if orig_el else ""
                final = final_el.get_text(strip=True) if final_el else ""
                
                game = make_game(
                    "playstation", game_id, title, discount,
                    link, orig, final, image_url,
                    is_free_to_keep=(discount == 100 and "free" in final.lower())
                )
                games.append(game)
                log.info(f"  🎮 PS Deal: {title} -{discount}%")
                
            except Exception as e:
                continue
                
    except Exception as e:
        log.error(f"  ❌ PlayStation deals parse error: {e}")

    log.info(f"  ✅ PlayStation deals: {len(games)} games found")
    return games

def playstation_get_promo_info(game: dict) -> tuple[str, str]:
    if not game.get("link"):
        return "Unknown", f"END:{current_week_anchor()}"
    r = safe_get(game["link"], retries=2, delay=1, use_scraper=True,
                 extra_headers={"Referer": "https://store.playstation.com/"})
    if r:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for el in soup.select(".offer-end, .countdown, [data-testid='offer-end']"):
                text = el.get_text(strip=True)
                match = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
                if match:
                    display = match.group(1)
                    return display, f"END:{display}"
                match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
                if match:
                    display = match.group(1)
                    return display, f"END:{display}"
        except Exception as e:
            pass
    return "Unknown", f"END:{current_week_anchor()}"

# ═══════════════════════════════════════════════════
#  AAA DETECTION WITH METACRITIC
# ═══════════════════════════════════════════════════

def is_aaa_game_metacritic(title: str) -> bool:
    """
    تشخیص بازی AAA با استفاده از Metacritic
    - اگر RAWG_API_KEY تنظیم شده باشد، از متاکریتیک استفاده می‌کند
    - در غیر این صورت از لیست دستی استفاده می‌کند
    """
    # اگر RAWG_API_KEY تنظیم شده باشد
    if RAWG_API_KEY:
        rawg = rawg_search(title)
        if rawg:
            metacritic = rawg.get("metacritic")
            rating_pct = rawg.get("rating_pct")
            ratings_count = rawg.get("ratings_count", 0)
            
            # شرط AAA: متاکریتیک ≥ 75 یا امتیاز RAWG ≥ 80%
            if metacritic and metacritic >= AAA_METACRITIC_THRESHOLD:
                return True
            if rating_pct and rating_pct >= AAA_RATING_THRESHOLD:
                return True
            # بازی‌های پرطرفدار با بیش از 2000 نقد
            if ratings_count > AAA_REVIEWS_THRESHOLD:
                return True
    
    # Fallback: لیست دستی (اگر RAWG_API_KEY تنظیم نشده باشد)
    aaa_list = [
        # بازی‌های AAA معروف
        "halo", "forza", "gears of war", "starfield", "doom",
        "cyberpunk", "witcher", "red dead", "gta", "assassin's creed",
        "final fantasy", "resident evil", "god of war", "spider-man",
        "horizon", "uncharted", "last of us", "ghost of tsushima",
        "call of duty", "battlefield", "far cry", "diablo", "overwatch",
        "fallout", "elder scrolls", "minecraft", "age of empires",
        "dead space", "mass effect", "dragon age", "batman",
        "arkham", "tomb raider", "wolfenstein", "prey", "dishonored",
        "dead cells", "hades", "cuphead", "hollow knight",
        "star wars", "jedi", "sniper", "ghost recon", "division",
        "watch dogs", "immortals", "fenyx", "ride", "mafia",
        "saints row", "crisis", "dragons dogma", "devil may cry",
        "monster hunter", "street fighter", "tekken", "mortal kombat",
        "crash bandicoot", "spyro", "ratchet and clank", "jak and daxter",
        "sly cooper", "infamous", "prototype", "darksiders",
        "borderlands", "bioshock", "portal", "half-life", "counter-strike",
        "team fortress", "left 4 dead", "dead rising", "dying light",
        "just cause", "sleeping dogs", "yakuza", "persona", "dragon quest"
    ]
    return any(aaa in title.lower() for aaa in aaa_list)

def enrich_from_metacritic(game: dict) -> bool:
    """تکمیل اطلاعات بازی از Metacritic از طریق RAWG"""
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
    
    return True

# ═══════════════════════════════════════════════════
#  PS PLUS ESSENTIAL
# ═══════════════════════════════════════════════════

def fetch_playstation_plus_essential() -> list[dict]:
    """گرفتن بازی‌های PS Plus Essential از RSS وبلاگ رسمی PlayStation"""
    games = []
    log.info("  🔍 Fetching PS Plus Essential from official PlayStation Blog RSS")
    
    try:
        rss_url = "https://blog.playstation.com/feed/"
        feed = feedparser.parse(rss_url)
        
        for entry in feed.entries[:30]:
            title = entry.title
            if "PlayStation Plus" not in title and "PS Plus" not in title:
                continue
            
            content = entry.content[0].value if hasattr(entry, 'content') else entry.description
            full_text = title + " " + content
            
            # روش اول: جستجوی الگوی "Game A, Game B, and Game C"
            pattern1 = r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*,\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*(?:,?\s*and\s*|\s*&\s*)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)'
            matches = re.findall(pattern1, full_text)
            
            if matches:
                for match in matches[0]:
                    game_name = match.strip()
                    if len(game_name) > 3 and not any(x in game_name.lower() for x in ["plus", "playstation", "member", "download", "month"]):
                        game_id = game_name.lower().replace(" ", "-").replace(":", "").replace("'", "")
                        games.append(make_game(
                            "playstation_essential", game_id, game_name, 100,
                            entry.link, "", "FREE (PS Plus Essential)", "",
                            is_free_to_keep=True
                        ))
                if games:
                    log.info(f"    Found {len(games)} games from RSS")
                    return games
            
            # روش دوم: جستجوی نام بازی‌های معروف
            famous_games = [
                "God of War", "Horizon", "Uncharted", "The Last of Us",
                "Final Fantasy", "Resident Evil", "Cyberpunk", "The Witcher",
                "Red Dead", "GTA", "Assassin's Creed", "Far Cry",
                "Ghost of Tsushima", "Death Stranding", "Days Gone",
                "Demon's Souls", "Ratchet and Clank", "Returnal",
                "Spider-Man", "Diablo", "Overwatch", "Doom",
                "Fallout", "Elder Scrolls", "Minecraft", "Age of Empires"
            ]
            
            for game in famous_games:
                if game.lower() in full_text.lower():
                    game_id = game.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    games.append(make_game(
                        "playstation_essential", game_id, game, 100,
                        entry.link, "", "FREE (PS Plus Essential)", "",
                        is_free_to_keep=True
                    ))
            
    except Exception as e:
        log.warning(f"  ⚠️ RSS error: {e}")
    
    # اگر هیچ بازی پیدا نشد، از یک منبع ثابت استفاده کن
    if not games:
        log.info("  🔍 Fallback: using static list of current PS Plus games")
        current_games = [
            ("God of War Ragnarök", "god-of-war-ragnarok"),
            ("The Last of Us Part I", "the-last-of-us-part-i"),
            ("Final Fantasy VII Remake", "final-fantasy-vii-remake"),
        ]
        for title, game_id in current_games:
            games.append(make_game(
                "playstation_essential", game_id, title, 100,
                "https://blog.playstation.com", "", "FREE (PS Plus Essential)", "",
                is_free_to_keep=True
            ))
    
    log.info(f"  ✅ PS Essential: {len(games)} games found")
    return games

# ═══════════════════════════════════════════════════
#  PS PLUS EXTRA
# ═══════════════════════════════════════════════════

def fetch_playstation_plus_extra() -> list[dict]:
    """گرفتن بازی‌های PS Plus Extra از RSS وبلاگ رسمی PlayStation"""
    games = []
    log.info("  🔍 Fetching PS Plus Extra from official PlayStation Blog RSS")
    
    try:
        rss_url = "https://blog.playstation.com/feed/"
        feed = feedparser.parse(rss_url)
        
        for entry in feed.entries[:30]:
            title = entry.title
            if "PS Plus Extra" not in title and "PlayStation Plus Extra" not in title:
                continue
            
            content = entry.content[0].value if hasattr(entry, 'content') else entry.description
            full_text = title + " " + content
            
            # بازی‌های AAA معروف
            aaa_games = [
                "God of War", "Horizon", "Uncharted", "The Last of Us",
                "Final Fantasy", "Resident Evil", "Cyberpunk", "The Witcher",
                "Red Dead", "GTA", "Assassin's Creed", "Far Cry",
                "Ghost of Tsushima", "Death Stranding", "Days Gone",
                "Demon's Souls", "Ratchet and Clank", "Returnal",
                "Spider-Man", "Diablo", "Overwatch", "Doom",
                "Fallout", "Elder Scrolls", "Minecraft", "Age of Empires",
                "Dead Space", "Mass Effect", "Dragon Age", "Batman"
            ]
            
            for game in aaa_games:
                if game.lower() in full_text.lower():
                    game_id = game.lower().replace(" ", "-").replace(":", "").replace("'", "")
                    games.append(make_game(
                        "playstation_extra", game_id, game, 0,
                        entry.link, "", "Included in PS Plus Extra", "",
                        is_free_to_keep=False
                    ))
            
    except Exception as e:
        log.warning(f"  ⚠️ RSS Extra error: {e}")
    
    # اگر هیچ بازی پیدا نشد، از یک منبع ثابت استفاده کن
    if not games:
        log.info("  🔍 Fallback: using static list of current PS Plus Extra games")
        current_extra_games = [
            ("God of War Ragnarök", "god-of-war-ragnarok"),
            ("Horizon Forbidden West", "horizon-forbidden-west"),
            ("The Last of Us Part I", "the-last-of-us-part-i"),
        ]
        for title, game_id in current_extra_games:
            games.append(make_game(
                "playstation_extra", game_id, title, 0,
                "https://blog.playstation.com", "", "Included in PS Plus Extra", "",
                is_free_to_keep=False
            ))
    
    log.info(f"  ✅ PS Extra: {len(games)} games found")
    return games

# ═══════════════════════════════════════════════════
#  XBOX GAME PASS — با Metacritic برای تشخیص AAA
# ═══════════════════════════════════════════════════

def fetch_xbox_gamepass() -> list[dict]:
    """گرفتن بازی‌های Xbox Game Pass با تشخیص AAA از Metacritic"""
    games = []
    log.info("  🔍 Fetching Xbox Game Pass with Metacritic detection")
    
    # لیست گسترده بازی‌های Game Pass با Steam ID
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
        # بررسی AAA با Metacritic
        if not is_aaa_game_metacritic(title):
            log.debug(f"  ⏭️ Skipping {title} (not AAA)")
            continue
        
        # تصویر از استیم
        image_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{steam_id}/capsule_616x353.jpg"
        
        # اگر steam_id صفر باشد، از RAWG استفاده کن
        if steam_id == 0:
            rawg = rawg_search(title)
            if rawg and rawg.get("background_image"):
                image_url = rawg["background_image"]
        
        game = make_game(
            "xbox_gamepass", 
            slug, 
            title, 
            0,
            "https://www.xbox.com/en-US/xbox-game-pass", 
            "", 
            "Included in Game Pass", 
            image_url,
            is_free_to_keep=False
        )
        games.append(game)
        log.info(f"  🟩 Xbox Game Pass (AAA): {title}")
    
    log.info(f"  ✅ Xbox Game Pass: {len(games)} AAA games")
    return games

# ═══════════════════════════════════════════════════
#  STEAM SEARCH ENRICH
# ═══════════════════════════════════════════════════
def steam_search_by_title(title: str) -> str | None:
    r = safe_get(
        "https://store.steampowered.com/search/",
        params={"term": title, "cc": "US", "l": "english"},
        retries=2, delay=1
    )
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
                if title_el:
                    result_title = title_el.text.strip().lower()
                    if title.lower() in result_title or result_title in title.lower():
                        return appid
    except Exception as e:
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
        raw = details.get("short_description", "")
        game["description"] = BeautifulSoup(raw, "html.parser").get_text() if raw else ""
    if not game.get("genres"):
        game["genres"] = [g["description"] for g in details.get("genres", [])]
    if not game.get("review_pct"):
        rev_pct, rev_count, rev_desc = steam_get_reviews(appid)
        if rev_pct is not None:
            game["review_pct"] = rev_pct
            game["review_count"] = rev_count
            game["review_desc"] = rev_desc
    game["steam_image"] = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"
    return True

# ═══════════════════════════════════════════════════
#  CAPTION BUILDER
# ═══════════════════════════════════════════════════
def build_caption(game: dict, end_date: str) -> str:
    store    = game["store"]
    meta     = STORE_META[store]
    is_ftk   = game.get("is_free_to_keep", False)
    discount = game["discount"]
    title    = game["title"]

    raw_desc = game.get("description") or "No description available."
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc     = raw_desc[:260].rstrip() + ("…" if len(raw_desc) > 260 else "")

    rev_pct   = game.get("review_pct")
    rev_count = game.get("review_count")
    rev_desc  = game.get("review_desc", "")
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

    genres    = game.get("genres") or []
    genre_str = ", ".join(genres[:4]) if genres else None

    orig  = game.get("price_orig_fmt", "")
    final = game.get("price_final_fmt", "")

    if discount == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block  = (
            "100% OFF 🎁 <b>Free to Keep!</b>"
            if is_ftk else
            "100% OFF 🎁 <b>Now free!</b>"
        )
    else:
        price_block = (
            f"<s>{orig}</s> → <b>{final}</b>"
            if orig and final else (final or orig or "?")
        )
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
        f"📅 <b>Detected:</b> {now_utc} UTC",
        f"⏳ <b>Offer ends:</b> {end_date}",
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
            new_lines.append(short if line == desc else line)
        caption = "\n".join(new_lines)

    if len(caption) > 1024:
        new_lines, skip = [], False
        for line in lines:
            if line == "📝 <b>About:</b>":
                skip = True
                continue
            if skip and line == "":
                skip = False
                continue
            if not skip:
                new_lines.append(line)
        caption = "\n".join(new_lines)[:1024]

    return caption

# ═══════════════════════════════════════════════════
#  TELEGRAM SENDER
# ═══════════════════════════════════════════════════
def send_game(game: dict, caption: str) -> bool:
    url        = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    candidates = get_image_candidates(game)
    candidates = [c for c in candidates if c]

    for img in candidates:
        try:
            r = requests.post(url, data={
                "chat_id":    CHANNEL,
                "photo":      img,
                "caption":    caption,
                "parse_mode": "HTML",
            }, timeout=30)
            result = r.json()
            if result.get("ok"):
                return True
            err = result.get("description", "")
            log.warning(f"Telegram error: {err}")
            if "can't parse" in err.lower():
                clean = BeautifulSoup(caption, "html.parser").get_text()
                r2 = requests.post(url, data={
                    "chat_id": CHANNEL, "photo": img, "caption": clean[:1024],
                }, timeout=30)
                if r2.json().get("ok"):
                    return True
            if "wrong type" in err or "failed" in err:
                continue
        except Exception as e:
            log.error(f"Send exception: {e}")

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHANNEL, "text": caption, "parse_mode": "HTML"},
            timeout=30,
        )
        if r.json().get("ok"):
            return True
    except Exception as e:
        log.error(f"sendMessage fallback failed: {e}")

    return False

# ═══════════════════════════════════════════════════
#  PROCESS ONE GAME
# ═══════════════════════════════════════════════════
def process_game(game: dict) -> tuple[str, str]:
    store  = game["store"]
    gid    = game["id"]
    title  = game["title"]
    is_ftk = game.get("is_free_to_keep", False)

    if is_recently_sent_cached(store, gid):
        return "skipped", "sent within last 24 hours (cache)"

    if store == "steam":
        details = steam_get_details(gid)
        if not details:
            return "failed", "no details"
        if details.get("is_free", False) and not is_ftk:
            return "invalid", "free-to-play permanent"
        app_type = details.get("type", "")
        if app_type not in ("game", ""):
            return "invalid", f"type={app_type}"
        disc = game["discount"]
        if disc < MIN_DISCOUNT and disc != 100:
            return "invalid", f"discount {disc}% < {MIN_DISCOUNT}%"
        po = details.get("price_overview") or {}
        if po:
            game["price_orig_fmt"]  = po.get("initial_formatted", game.get("price_orig_fmt", ""))
            game["price_final_fmt"] = po.get("final_formatted",   game.get("price_final_fmt", ""))
            game["discount"]        = po.get("discount_percent",  game["discount"])
        raw = details.get("short_description", "")
        game["description"] = BeautifulSoup(raw, "html.parser").get_text() if raw else ""
        game["genres"]      = [g["description"] for g in details.get("genres", [])]
        rev_pct, rev_count, rev_desc = steam_get_reviews(gid)
        game["review_pct"]   = rev_pct
        game["review_count"] = rev_count
        game["review_desc"]  = rev_desc
        end_display, period_anchor = steam_get_promo_info(gid, is_ftk)

    elif store == "epic":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
        end_display, period_anchor = epic_get_promo_info(game)

    elif store == "gog":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
        end_display, period_anchor = gog_get_promo_info(game)

    elif store == "playstation":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        end_display, period_anchor = playstation_get_promo_info(game)

    elif store == "playstation_essential":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"🎮 {game['title']} is FREE with PS Plus Essential this month!"
        end_display = "End of month"
        period_anchor = f"MONTH:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    elif store == "playstation_extra":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"🎮 {game['title']} is included in PS Plus Extra!"
        if is_recently_sent_db(store, gid, days=365):
            return "skipped", "sent within last year"
        end_display = "Included in Extra"
        period_anchor = f"EXTRA:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    elif store == "xbox_gamepass":
        enrich_from_steam(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_metacritic(game)
        if not game.get("genres"):
            game["genres"] = ["Action", "Adventure"]
        if not game.get("description"):
            game["description"] = f"🎮 {game['title']} is available on Xbox Game Pass. Experience this amazing game with your Game Pass subscription!"
        if not game.get("review_pct"):
            game["review_pct"] = 85
            game["review_count"] = 5000
            game["review_desc"] = "Highly Rated"
        if is_recently_sent_db(store, gid, days=365):
            return "skipped", "sent within last year"
        end_display = "Included in Game Pass"
        period_anchor = f"GAMEPASS:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    else:
        return "failed", f"unknown store {store}"

    deal_hash = game.get("deal_hash", "")
    if not deal_hash:
        deal_hash = get_deal_hash(game)
    if not deal_hash:
        deal_hash = make_promo_key(store, gid, period_anchor)

    if not is_deal_changed(store, gid, deal_hash):
        return "skipped", "no change in deal"

    if is_sent(store, gid, deal_hash):
        return "skipped", "already sent this deal"

    caption = build_caption(game, end_display)
    ok      = send_game(game, caption)

    if ok:
        mark_sent(store, gid, title, deal_hash)
        mark_sent_cached(store, gid)
        return "sent", ""
    else:
        return "failed", "telegram send error"

# ═══════════════════════════════════════════════════
#  MAIN — با ارسال چرخشی (PC → PS → Xbox)
# ═══════════════════════════════════════════════════

def main():
    log.info("═" * 65)
    log.info("  🎮 FreeGamesHub — Steam + Epic + GOG + PlayStation + Xbox")
    log.info("═" * 65)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    if not RAWG_API_KEY:
        log.warning("⚠️  RAWG_API_KEY not set — some stores may lack genre/description")

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

    log.info(f"Total unique deals: {len(all_games)}")

    if not all_games:
        log.warning("No games found — exiting")
        return

    # ─── مرتب‌سازی اولیه ──────────────────────────────────────────
    # اولویت: FTK > 100% > تخفیف بیشتر
    all_games.sort(key=lambda x: (
        x.get("is_free_to_keep", False),
        x["discount"] == 100,
        x["discount"],
    ), reverse=True)

    # ─── گروه‌بندی بر اساس فروشگاه ──────────────────────────────
    pc_stores = ["steam", "epic", "gog"]
    ps_stores = ["playstation", "playstation_essential", "playstation_extra"]
    xbox_stores = ["xbox_gamepass"]

    pc_games = [g for g in all_games if g["store"] in pc_stores]
    ps_games = [g for g in all_games if g["store"] in ps_stores]
    xbox_games_filtered = [g for g in all_games if g["store"] in xbox_stores]

    # ─── مرتب‌سازی هر گروه (FTK اول) ─────────────────────────────
    pc_games.sort(key=lambda x: (not x.get("is_free_to_keep", False), -x["discount"]))
    ps_games.sort(key=lambda x: (not x.get("is_free_to_keep", False), -x["discount"]))
    xbox_games_filtered.sort(key=lambda x: (not x.get("is_free_to_keep", False), -x["discount"]))

    log.info(f"  📊 Grouped: PC={len(pc_games)}, PS={len(ps_games)}, Xbox={len(xbox_games_filtered)}")

    # ─── ارسال چرخشی (Round-Robin) ──────────────────────────────
    # PC → PS → Xbox → PC → PS → Xbox → ...
    groups = [
        ("PC", pc_games),
        ("PS", ps_games),
        ("Xbox", xbox_games_filtered),
    ]

    # حذف گروه‌های خالی
    groups = [(name, games) for name, games in groups if games]

    counters = {"sent": 0, "skipped": 0, "invalid": 0, "failed": 0}
    total_games = sum(len(games) for _, games in groups)
    sent_count = 0

    # ارسال چرخشی تا زمانی که همه بازی‌ها ارسال شوند
    while sent_count < total_games:
        for group_name, games in groups:
            if not games:
                continue
            
            # یک بازی از این گروه بردار
            game = games.pop(0)
            sent_count += 1
            
            store = game["store"].upper()
            label = "🎁 FTK" if game.get("is_free_to_keep") else f"-{game['discount']}%"
            log.info(f"[{sent_count:3}/{total_games}] [{group_name:<4}][{store:<5}] {game['title'][:40]:<40} | {label}")

            status, reason = process_game(game)
            counters[status] = counters.get(status, 0) + 1

            if status == "sent":
                log.info("       ✅ Sent")
            elif status == "skipped":
                log.info(f"       ⏭  Skipped — {reason}")
            elif status == "invalid":
                log.info(f"       ⚠️  Invalid — {reason}")
            else:
                log.error(f"       ❌ Failed — {reason}")

            time.sleep(3)  # تأخیر بین ارسال‌ها

    log.info("═" * 65)
    log.info(f"  ✅ Sent:    {counters['sent']}")
    log.info(f"  ⏭  Skipped: {counters['skipped']}")
    log.info(f"  ⚠️  Invalid: {counters['invalid']}")
    log.info(f"  ❌ Failed:  {counters['failed']}")
    log.info("═" * 65)

# ═══════════════════════════════════════════════════
#  اجرای خودکار هر ۱۲ ساعت
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            log.exception("❗ خطای غیرمنتظره در حین اجرا:")
        log.info("⏳ منتظر ۱۲ ساعت تا اجرای بعدی...")
        time.sleep(12 * 3600)
