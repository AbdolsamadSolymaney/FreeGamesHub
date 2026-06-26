"""
FreeGamesHub — Steam + Epic Games Store + GOG + PlayStation + Xbox Game Pass
================================================================================
- دریافت خودکار تخفیف‌ها و بازی‌های رایگان از فروشگاه‌های معتبر
- ارسال به کانال تلگرام با تصویر استاندارد 616×353
- تکمیل اطلاعات (ژانر، توضیحات، نقدها) از RAWG و در صورت نیاز از Steam
- تشخیص تغییرات تخفیف با هش ترکیبی (شروع، پایان، قیمت) + کش ۲۴ ساعته برای جلوگیری از ارسال تکراری
- پشتیبانی از PlayStation Deals (≥۷۵٪), PS Plus Essential (ماهانه), PS Plus Extra (AAA جدید)
- پشتیبانی از Xbox Game Pass Standard (AAA جدید)
- اجرای خودکار هر ۱۲ ساعت
- حداقل تخفیف: ۷۵٪ (قابل تنظیم)
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
    "PlayStation Plus", "PS Plus", "Xbox Game Pass", "Game Pass",  # حذف خود سرویس‌ها
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
    cache_key = title.lower().strip()
    if cache_key in _rawg_cache:
        return _rawg_cache[cache_key]

    clean = re.sub(r"[™®©]", "", title).strip()
    clean = re.sub(r"\s*[\(\[\{].*?[\)\]\}]", "", clean).strip()

    params = {"search": clean, "page_size": 5}
    if RAWG_API_KEY:
        params["key"] = RAWG_API_KEY

    r = safe_get(
        "https://api.rawg.io/api/games",
        params=params,
        extra_headers={"Referer": "https://rawg.io/"},
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
            log.debug(f"RAWG: no good match for '{title}' (best={best_score:.0f})")
            _rawg_cache[cache_key] = None
            return None

        description = ""
        detail_params = {"key": RAWG_API_KEY} if RAWG_API_KEY else {}
        dr = safe_get(
            f"https://api.rawg.io/api/games/{best['id']}",
            params=detail_params,
            extra_headers={"Referer": "https://rawg.io/"},
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
        log.info(f"  🎲 RAWG match: '{title}' → '{best.get('name')}' (score={best_score:.0f})")
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

    if store == "steam":
        return [
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{gid}/capsule_616x353.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{gid}/capsule_616x353.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{gid}/header.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{gid}/header.jpg",
        ]
    else:
        candidates = []
        if game.get("rawg_image"):
            candidates.append(game["rawg_image"])
        if game.get("image_url"):
            # تبدیل تصویر کوچک PS به بزرگ‌تر
            if "playstation" in game.get("store", ""):
                # افزایش کیفیت تصویر پلی‌استیشن
                img = game["image_url"]
                img = img.replace("_small", "_large")
                img = img.replace("_thumb", "_original")
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
#  GOG
# ═══════════════════════════════════════════════════
def fetch_gog_games() -> list[dict]:
    games = []
    gog_sales  = _gog_fetch_sales()
    _merge(games, gog_sales)
    log.info(f"GOG Source 1 (Sales):      {len(gog_sales)}")

    gog_free = _gog_fetch_free()
    before   = len(games)
    _merge(games, gog_free)
    log.info(f"GOG Source 2 (Free):       {len(gog_free)} raw → {len(games)-before} new")
    return games

def _gog_fetch_sales() -> list[dict]:
    games = []
    r = safe_get(
        "https://catalog.gog.com/v1/catalog",
        params={
            "limit":      48,
            "order":      "desc:trending",
            "discounted": "true",
            "productType": "in:game",
            "page":       1,
        },
        extra_headers={"Referer": "https://www.gog.com/"},
    )
    if not r:
        return games
    try:
        data = r.json()
        for item in data.get("products", []):
            title = item.get("title", "").strip()
            if not title or _should_skip(title):
                continue

            price_info = item.get("price", {}) or {}
            discount   = int(price_info.get("discountPercentage", 0) or 0)
            if discount < MIN_DISCOUNT and discount != 100:
                continue

            orig_raw   = float(price_info.get("base",  0) or 0)
            final_raw  = float(price_info.get("final", 0) or 0)
            orig_fmt   = f"${orig_raw:.2f}"  if orig_raw  else ""
            final_fmt  = f"${final_raw:.2f}" if final_raw else ("FREE" if discount == 100 else "")

            slug     = item.get("slug", "")
            link     = f"https://www.gog.com/en/game/{slug}" if slug else "https://www.gog.com"
            cover    = item.get("coverHorizontal", "") or item.get("cover", "")
            game_id  = str(item.get("id", slug or title))
            is_ftk   = (discount == 100 and final_raw == 0)

            game = make_game(
                "gog", game_id, title, discount,
                link, orig_fmt, final_fmt,
                image_url=cover,
                is_free_to_keep=is_ftk,
            )
            games.append(game)
            if is_ftk:
                log.info(f"  🟣 GOG Free: {title}")
            else:
                log.info(f"  🟣 GOG Sale: {title} -{discount}%")

    except Exception as e:
        log.error(f"GOG sales parse error: {e}")
    return games

def _gog_fetch_free() -> list[dict]:
    games = []
    r = safe_get(
        "https://www.gog.com/en/games",
        params={"priceRange": "0,0", "discounted": "true"},
        extra_headers={"Referer": "https://www.gog.com/"},
        use_scraper=True,
    )
    if not r:
        return games
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        for card in soup.select("[data-test-id='productCard'], .product-tile, [class*='productCard']"):
            try:
                title_el = (
                    card.select_one("[data-test-id='productTitle']")
                    or card.select_one(".product-tile__title")
                    or card.select_one("[class*='productTitle']")
                )
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                if not title or _should_skip(title):
                    continue

                link_el = card.select_one("a[href*='/game/'], a[href*='/en/game/']")
                if not link_el:
                    continue
                href    = link_el.get("href", "")
                slug    = href.rstrip("/").split("/")[-1]
                link    = f"https://www.gog.com/en/game/{slug}"
                game_id = slug or title

                img_el    = card.select_one("img")
                image_url = img_el.get("src", "") if img_el else ""

                price_el = (
                    card.select_one("[data-test-id='finalPrice']")
                    or card.select_one(".product-tile__price-final")
                )
                if price_el:
                    price_text = price_el.get_text(strip=True).lower()
                    if "free" not in price_text and price_text not in ("$0.00", "0", "0.00"):
                        continue

                games.append(make_game(
                    "gog", game_id, title, 100,
                    link, orig_fmt="", final_fmt="FREE",
                    image_url=image_url,
                    is_free_to_keep=True,
                ))
                log.info(f"  🟣 GOG Free (HTML): {title}")
            except Exception as e:
                log.debug(f"GOG card parse error: {e}")
    except Exception as e:
        log.error(f"GOG free HTML error: {e}")
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
#  PLAYSTATION — اسکرپینگ اصلاح‌شده
# ═══════════════════════════════════════════════════
def fetch_playstation_deals() -> list[dict]:
    """بازی‌های با تخفیف ≥۷۵٪ از فروشگاه PlayStation"""
    games = []
    url = "https://store.playstation.com/en-us/deals"
    r = safe_get(url, use_scraper=True, retries=3, delay=3,
                 extra_headers={"Referer": "https://store.playstation.com/"})
    if not r:
        log.error("  ❌ PlayStation deals page not accessible")
        return games

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # سلکتورهای دقیق‌تر برای بازی‌ها
        products = (
            soup.select("[data-testid='product-card']") or
            soup.select(".product-card") or
            soup.select("[class*='product']") or
            soup.select("li[data-product-id]")
        )
        if not products:
            log.warning("  ⚠️ No products found, trying fallback")
            products = soup.select("[class*='game']")

        for product in products:
            try:
                # عنوان بازی
                title_el = (
                    product.select_one("[data-testid='product-title']") or
                    product.select_one(".product-title") or
                    product.select_one("h3, h2")
                )
                if not title_el:
                    continue
                title = title_el.text.strip()
                if not title or _should_skip(title):
                    continue

                # درصد تخفیف
                discount_el = (
                    product.select_one("[data-testid='discount-badge']") or
                    product.select_one(".discount-badge") or
                    product.select_one("[class*='discount']")
                )
                if not discount_el:
                    continue
                disc_text = discount_el.text.strip().replace("%", "").replace("-", "")
                discount = int(disc_text) if disc_text.isdigit() else 0
                if discount < MIN_DISCOUNT:
                    continue

                # قیمت‌ها
                orig_el = (
                    product.select_one("[data-testid='original-price']") or
                    product.select_one(".original-price") or
                    product.select_one(".price__old")
                )
                final_el = (
                    product.select_one("[data-testid='final-price']") or
                    product.select_one(".final-price") or
                    product.select_one(".price__current")
                )
                orig = orig_el.text.strip() if orig_el else ""
                final = final_el.text.strip() if final_el else ""

                # لینک
                link_el = product.select_one("a[href*='/product/']")
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://store.playstation.com" + link

                # تصویر (کیفیت بالا)
                img_el = product.select_one("img")
                image = img_el.get("src", "") if img_el else ""
                if image and image.startswith("//"):
                    image = "https:" + image
                # افزایش کیفیت
                if image:
                    image = image.replace("_small", "_large")
                    image = image.replace("_thumb", "_original")
                    image = re.sub(r'/\d+x\d+/', '/original/', image)

                game_id = ""
                if link:
                    match = re.search(r"/product/([^/?]+)", link)
                    if match:
                        game_id = match.group(1)
                if not game_id:
                    game_id = title.lower().replace(" ", "-")

                game = make_game(
                    "playstation", game_id, title, discount,
                    link, orig, final, image,
                    is_free_to_keep=(discount == 100 and final.lower() == "free")
                )
                games.append(game)
                log.info(f"  🎮 PS Deal: {title} -{discount}%")

            except Exception as e:
                log.debug(f"  ⚠️ Product parse error: {e}")
                continue

    except Exception as e:
        log.error(f"  ❌ PlayStation deals parse error: {e}")

    log.info(f"  ✅ PlayStation deals: {len(games)} games found")
    return games

def fetch_playstation_plus_essential() -> list[dict]:
    """بازی‌های ماهانه PS Plus Essential - اصلاح سلکتورها"""
    games = []
    url = "https://www.playstation.com/en-us/ps-plus/"
    r = safe_get(url, use_scraper=True, retries=3, delay=3,
                 extra_headers={"Referer": "https://www.playstation.com/"})
    if not r:
        log.error("  ❌ PS Plus page not accessible")
        return games

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        # پیدا کردن بازی‌ها با سلکتورهای دقیق
        # معمولاً بازی‌ها در بخش با کلاس "game-card" یا "ps-plus-game" قرار دارند
        game_cards = (
            soup.select(".ps-plus-game-card") or
            soup.select(".game-card") or
            soup.select("[class*='game-card']") or
            soup.select("[class*='game-tile']")
        )
        
        if not game_cards:
            log.warning("  ⚠️ No game cards found, trying alternative")
            # تلاش با پیدا کردن آیتم‌های حاوی "game" اما نه "plus" (برای حذف خود سرویس)
            all_cards = soup.select("[class*='game']")
            game_cards = [c for c in all_cards if "plus" not in c.get("class", []) and "service" not in c.get("class", [])]

        for card in game_cards[:5]:
            try:
                # عنوان بازی - حذف عناوین اشتباه
                title_el = (
                    card.select_one(".game-title") or
                    card.select_one("h3, h4") or
                    card.select_one("[class*='title']")
                )
                if not title_el:
                    continue
                
                title = title_el.text.strip()
                # نادیده گرفتن عناوین سرویس
                if not title or "PlayStation Plus" in title or "PS Plus" in title:
                    continue
                if _should_skip(title):
                    continue

                # لینک
                link_el = card.select_one("a[href]")
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.playstation.com" + link

                # تصویر با کیفیت بالا
                img_el = card.select_one("img")
                image = img_el.get("src", "") if img_el else ""
                if image and image.startswith("//"):
                    image = "https:" + image
                if image:
                    image = image.replace("_small", "_large")
                    image = image.replace("_thumb", "_original")
                    image = re.sub(r'/\d+x\d+/', '/original/', image)

                game_id = title.lower().replace(" ", "-").replace(":", "")

                game = make_game(
                    "playstation_essential", game_id, title, 100,
                    link, "", "FREE (PS Plus Essential)", image,
                    is_free_to_keep=True
                )
                games.append(game)
                log.info(f"  🎮 PS Essential: {title}")

            except Exception as e:
                log.debug(f"  ⚠️ Essential card parse error: {e}")
                continue

    except Exception as e:
        log.error(f"  ❌ PS Essential parse error: {e}")

    log.info(f"  ✅ PS Essential: {len(games)} games found")
    return games

def fetch_playstation_plus_extra() -> list[dict]:
    """بازی‌های جدید PS Plus Extra (فقط AAA) - اصلاح سلکتورها"""
    games = []
    url = "https://www.playstation.com/en-us/ps-plus/"
    r = safe_get(url, use_scraper=True, retries=3, delay=3,
                 extra_headers={"Referer": "https://www.playstation.com/"})
    if not r:
        log.error("  ❌ PS Plus page not accessible")
        return games

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        # پیدا کردن بازی‌های Extra
        game_cards = (
            soup.select(".ps-plus-extra .game-card") or
            soup.select(".ps-plus-extra [class*='game']") or
            soup.select("[data-testid='extra-games'] .game-card") or
            soup.select("[class*='extra'] [class*='game']")
        )
        
        if not game_cards:
            # تلاش با تمام کارت‌ها و فیلتر دستی
            all_cards = soup.select("[class*='game']")
            # فقط بازی‌هایی که در بخش "extra" یا "catalog" هستند
            game_cards = [c for c in all_cards if any(x in str(c.parent) for x in ["extra", "catalog", "new"])]

        for card in game_cards[:10]:
            try:
                title_el = (
                    card.select_one(".game-title") or
                    card.select_one("h3, h4") or
                    card.select_one("[class*='title']")
                )
                if not title_el:
                    continue
                
                title = title_el.text.strip()
                if not title or "PlayStation Plus" in title or "PS Plus" in title:
                    continue
                if _should_skip(title):
                    continue

                # تشخیص AAA
                rawg = rawg_search(title)
                is_aaa = False
                if rawg:
                    metacritic = rawg.get("metacritic")
                    if metacritic and metacritic >= 70:
                        is_aaa = True
                    elif rawg.get("rating_pct") and rawg.get("rating_pct") >= 70:
                        is_aaa = True

                if not is_aaa:
                    aaa_titles = [
                        "god of war", "spider-man", "horizon", "uncharted",
                        "last of us", "final fantasy", "resident evil",
                        "cyberpunk", "witcher", "red dead", "gta",
                        "call of duty", "battlefield", "assassin's creed",
                        "far cry", "ghost of tsushima", "death stranding",
                        "days gone", "demon's souls", "ratchet and clank",
                        "returnal", "diablo", "overwatch", "starfield",
                        "forza", "halo", "gears of war", "doom",
                        "fallout", "elder scrolls", "minecraft", "age of empires",
                        "stalker", "avowed", "fable", "perfect dark",
                        "metal gear", "silent hill", "devil may cry", "monster hunter"
                    ]
                    if any(aaa in title.lower() for aaa in aaa_titles):
                        is_aaa = True

                if not is_aaa and rawg and rawg.get("ratings_count", 0) > 1000:
                    is_aaa = True

                if not is_aaa:
                    log.debug(f"  ⏭️ Skipping non-AAA: {title}")
                    continue

                link_el = card.select_one("a[href]")
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.playstation.com" + link

                img_el = card.select_one("img")
                image = img_el.get("src", "") if img_el else ""
                if image and image.startswith("//"):
                    image = "https:" + image
                if image:
                    image = image.replace("_small", "_large")
                    image = image.replace("_thumb", "_original")
                    image = re.sub(r'/\d+x\d+/', '/original/', image)

                game_id = title.lower().replace(" ", "-").replace(":", "")

                game = make_game(
                    "playstation_extra", game_id, title, 0,
                    link, "", "Included in PS Plus Extra", image,
                    is_free_to_keep=False
                )
                games.append(game)
                log.info(f"  🎮 PS Extra (AAA): {title}")

            except Exception as e:
                log.debug(f"  ⚠️ Extra card parse error: {e}")
                continue

    except Exception as e:
        log.error(f"  ❌ PS Extra parse error: {e}")

    log.info(f"  ✅ PS Extra: {len(games)} games found")
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
            log.debug(f"  ⚠️ PlayStation date extraction error: {e}")
    return "Unknown", f"END:{current_week_anchor()}"

# ═══════════════════════════════════════════════════
#  XBOX GAME PASS — اصلاح کامل
# ═══════════════════════════════════════════════════
def fetch_xbox_gamepass() -> list[dict]:
    """بازی‌های جدید اضافه‌شده به Game Pass (فقط AAA) - اصلاح سلکتورها"""
    games = []
    urls = [
        "https://www.xbox.com/en-US/xbox-game-pass/games",
        "https://www.xbox.com/en-US/games/xbox-game-pass",
        "https://www.xbox.com/en-US/game-pass/games"
    ]
    
    r = None
    for url in urls:
        r = safe_get(url, use_scraper=True, retries=3, delay=3,
                     extra_headers={"Referer": "https://www.xbox.com/"})
        if r:
            break
    
    if not r:
        log.error("  ❌ Xbox Game Pass pages not accessible")
        return games

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        
        # پیدا کردن بازی‌ها با سلکتورهای مختلف
        game_cards = (
            soup.select(".game-card") or
            soup.select(".game-tile") or
            soup.select("[class*='game-card']") or
            soup.select("[class*='game-tile']") or
            soup.select("[data-testid='game-card']") or
            soup.select(".card") or
            soup.select("[class*='game']")
        )
        
        # فیلتر کردن عناصر غیربازی (مثل headerها)
        game_cards = [c for c in game_cards if c.select_one("a[href]") and c.select_one("img")]
        
        if not game_cards:
            log.warning("  ⚠️ No Xbox Game Pass games found")
            return games

        for card in game_cards[:15]:
            try:
                # عنوان
                title_el = (
                    card.select_one(".game-title") or
                    card.select_one("h3, h4, h2") or
                    card.select_one("[class*='title']") or
                    card.select_one("[class*='name']")
                )
                if not title_el:
                    continue
                
                title = title_el.text.strip()
                if not title or "Xbox" in title or "Game Pass" in title:
                    continue
                if _should_skip(title):
                    continue

                # تشخیص AAA (با آستانه پایین‌تر)
                rawg = rawg_search(title)
                is_aaa = False
                if rawg:
                    metacritic = rawg.get("metacritic")
                    if metacritic and metacritic >= 65:  # کاهش آستانه
                        is_aaa = True
                    elif rawg.get("rating_pct") and rawg.get("rating_pct") >= 65:
                        is_aaa = True
                    elif rawg.get("ratings_count", 0) > 500:  # بازی‌های پرطرفدار
                        is_aaa = True

                if not is_aaa:
                    # لیست گسترده بازی‌های معروف
                    famous_titles = [
                        "starfield", "forza", "halo", "gears of war",
                        "call of duty", "diablo", "overwatch", "doom",
                        "fallout", "elder scrolls", "minecraft", "age of empires",
                        "stalker", "avowed", "fable", "perfect dark",
                        "cyberpunk", "witcher", "red dead", "gta",
                        "assassin's creed", "far cry", "resident evil",
                        "final fantasy", "monster hunter", "devil may cry",
                        "dead space", "mass effect", "dragon age", "batman",
                        "arkham", "tomb raider", "wolfenstein", "prey",
                        "dishonored", "dead cells", "hades", "cuphead"
                    ]
                    if any(f in title.lower() for f in famous_titles):
                        is_aaa = True

                if not is_aaa:
                    log.debug(f"  ⏭️ Skipping non-AAA: {title}")
                    continue

                # لینک
                link_el = card.select_one("a[href]")
                link = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.xbox.com" + link

                # تصویر (سعی در گرفتن کیفیت بالا)
                img_el = card.select_one("img")
                image = img_el.get("src", "") if img_el else ""
                if image:
                    # تبدیل به کیفیت بالا
                    image = image.replace("_small", "_large")
                    image = image.replace("_thumb", "_full")
                    image = re.sub(r'/w\d+/', '/w2000/', image)

                game_id = title.lower().replace(" ", "-")

                game = make_game(
                    "xbox_gamepass", game_id, title, 0,
                    link, "", "Included in Game Pass", image,
                    is_free_to_keep=False
                )
                games.append(game)
                log.info(f"  🎮 Xbox Game Pass (AAA): {title}")

            except Exception as e:
                log.debug(f"  ⚠️ Xbox card parse error: {e}")
                continue

    except Exception as e:
        log.error(f"  ❌ Xbox Game Pass parse error: {e}")

    log.info(f"  ✅ Xbox Game Pass: {len(games)} games found")
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
        log.debug(f"  ⚠️ Steam search error for '{title}': {e}")
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
    # استفاده از تصویر استیم به عنوان اولویت
    if appid and not game.get("rawg_image"):
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
    
    # اولویت تصویر: steam_image (از استیم) > rawg_image > image_url
    if game.get("steam_image"):
        candidates.insert(0, game["steam_image"])

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

    # ── چک کش ۲۴ ساعته ──
    if is_recently_sent_cached(store, gid):
        return "skipped", "sent within last 24 hours (cache)"

    # ── Steam ──────────────────────────────────────────────────
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

    # ── Epic ──────────────────────────────────────────────────
    elif store == "epic":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
        end_display, period_anchor = epic_get_promo_info(game)

    # ── GOG ──────────────────────────────────────────────────
    elif store == "gog":
        enrich_epic_gog(game)
        if not game.get("genres") or not game.get("description"):
            enrich_from_steam(game)
        end_display, period_anchor = gog_get_promo_info(game)

    # ── PlayStation Deals ──────────────────────────────────
    elif store == "playstation":
        enrich_from_steam(game)
        end_display, period_anchor = playstation_get_promo_info(game)

    # ── PlayStation Plus Essential ──────────────────────────
    elif store == "playstation_essential":
        enrich_from_steam(game)
        end_display = "End of month"
        period_anchor = f"MONTH:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    # ── PlayStation Plus Extra ──────────────────────────────
    elif store == "playstation_extra":
        enrich_from_steam(game)
        if is_recently_sent_db(store, gid, days=365):
            return "skipped", "sent within last year"
        end_display = "Included in Extra"
        period_anchor = f"EXTRA:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    # ── Xbox Game Pass ──────────────────────────────────────
    elif store == "xbox_gamepass":
        enrich_from_steam(game)
        if is_recently_sent_db(store, gid, days=365):
            return "skipped", "sent within last year"
        end_display = "Included in Game Pass"
        period_anchor = f"GAMEPASS:{datetime.datetime.utcnow().strftime('%Y-%m')}"

    else:
        return "failed", f"unknown store {store}"

    # ── Dedup ──
    deal_hash = game.get("deal_hash", "")
    if not deal_hash:
        deal_hash = get_deal_hash(game)
    if not deal_hash:
        deal_hash = make_promo_key(store, gid, period_anchor)

    if not is_deal_changed(store, gid, deal_hash):
        return "skipped", "no change in deal"

    if is_sent(store, gid, deal_hash):
        return "skipped", "already sent this deal"

    # ── Send ──
    caption = build_caption(game, end_display)
    ok      = send_game(game, caption)

    if ok:
        mark_sent(store, gid, title, deal_hash)
        mark_sent_cached(store, gid)
        return "sent", ""
    else:
        return "failed", "telegram send error"

# ═══════════════════════════════════════════════════
#  MAIN
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

    all_games.sort(key=lambda x: (
        x.get("is_free_to_keep", False),
        x["discount"] == 100,
        x["discount"],
    ), reverse=True)

    counters = {"sent": 0, "skipped": 0, "invalid": 0, "failed": 0}

    for idx, game in enumerate(all_games, 1):
        store  = game["store"].upper()
        label  = "🎁 FTK" if game.get("is_free_to_keep") else f"-{game['discount']}%"
        log.info(f"[{idx:3}/{len(all_games)}] [{store:<5}] {game['title'][:45]:<45} | {label}")

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

        time.sleep(3)

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
