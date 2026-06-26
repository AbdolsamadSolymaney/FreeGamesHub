"""
FreeGamesHub — Steam + Epic Games Store + GOG
==============================================
منطق dedup:
  - کلید = hash(store + game_id + period_anchor)
  - اگه تاریخ پایان مشخص باشه  → anchor = "END:2025-08-15"
  - اگه Free to Keep با تاریخ  → anchor = "FTK:2025-07-01"
  - اگه تاریخ نامشخص باشه      → anchor = "WEEK:2025-W26"

فروشگاه‌ها:
  Steam  → تخفیف ≥90% + Free to Keep
  Epic   → فقط بازی‌های رایگان هفتگی (Free Games)
  GOG    → تخفیف ≥90% + GOG Free Games
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
DB_FILE      = "games.db"
MIN_DISCOUNT = 90

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
]

# store label → emoji + display name
STORE_META = {
    "steam": {"emoji": "🟦", "name": "Steam",           "tag": "#Steam"},
    "epic":  {"emoji": "⬛", "name": "Epic Games Store", "tag": "#EpicGames"},
    "gog":   {"emoji": "🟣", "name": "GOG",              "tag": "#GOG"},
}

# ═══════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            store      TEXT,
            game_id    TEXT,
            promo_key  TEXT,
            title      TEXT,
            sent_at    TEXT,
            PRIMARY KEY (store, game_id, promo_key)
        )
    """)
    conn.commit()
    conn.close()

def is_sent(store: str, game_id: str, promo_key: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM sent WHERE store=? AND game_id=? AND promo_key=?",
        (store, game_id, promo_key)
    ).fetchone()
    conn.close()
    return row is not None

def mark_sent(store: str, game_id: str, title: str, promo_key: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO sent (store, game_id, promo_key, title, sent_at) VALUES (?,?,?,?,?)",
        (store, game_id, promo_key, title, ts)
    )
    conn.commit()
    conn.close()

def make_promo_key(store: str, game_id: str, period_anchor: str) -> str:
    raw = f"{store}|{game_id}|{period_anchor}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def current_week_anchor() -> str:
    iso = datetime.datetime.utcnow().isocalendar()
    return f"WEEK:{iso[0]}-W{iso[1]:02d}"

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
    }

def _merge(base: list, new_items: list):
    seen = {(g["store"], g["id"]) for g in base}
    for g in new_items:
        key = (g["store"], g["id"])
        if key not in seen:
            base.append(g)
            seen.add(key)

# ═══════════════════════════════════════════════════
#  ██████╗ STEAM SOURCES
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
                f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
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
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
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
                    f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
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
                    image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
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
                        image_url=f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
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
    """تاریخ پایان تخفیف از صفحه HTML → (display, anchor)"""
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
#  ⬛ EPIC GAMES STORE
# ═══════════════════════════════════════════════════

EPIC_GQL_URL = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"

def fetch_epic_games() -> list[dict]:
    """
    Epic free games از API رسمی Epic.
    این API هر هفته بازی‌های رایگان رو + بازی هفته بعد رو برمی‌گردونه.
    """
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

            # فقط بازی‌های رایگان فعلی (نه upcoming)
            promotions = el.get("promotions") or {}
            promo_offers = promotions.get("promotionalOffers", [])
            if not promo_offers:
                continue

            # بررسی اینکه پروموشن الان فعاله
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

            # قیمت اصلی
            price_info  = el.get("price", {}) or {}
            total_price = price_info.get("totalPrice", {}) or {}
            orig_cents  = total_price.get("originalPrice", 0)
            orig_fmt    = f"${orig_cents/100:.2f}" if orig_cents else ""

            # تصویر
            image_url = ""
            for img in el.get("keyImages", []):
                if img.get("type") in ("DieselStoreFrontWide", "OfferImageWide", "Thumbnail"):
                    image_url = img.get("url", "")
                    break

            # لینک
            slug = (
                el.get("catalogNs", {}).get("mappings", [{}])[0].get("pageSlug", "")
                or el.get("productSlug", "")
                or el.get("urlSlug", "")
            )
            link = f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games"

            # آی‌دی یکتا
            game_id = el.get("id") or el.get("productSlug") or slug or title

            end_display = active_offer["end"].strftime("%Y-%m-%d")

            games.append(make_game(
                "epic", str(game_id), title, 100,
                link,
                orig_fmt=orig_fmt,
                final_fmt="FREE",
                image_url=image_url,
                is_free_to_keep=True,
            ))
            log.info(f"  ⬛ Epic Free: {title} (ends {end_display})")

    except Exception as e:
        log.error(f"Epic parse error: {e}")

    log.info(f"Epic Games total: {len(games)}")
    return games

def epic_get_promo_info(game: dict) -> tuple[str, str]:
    """
    تاریخ پایان از API Epic — دوباره call میکنیم برای game_id خاص.
    اگه پیدا نشد از هفته جاری استفاده میکنیم.
    """
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
#  🟣 GOG
# ═══════════════════════════════════════════════════

def fetch_gog_games() -> list[dict]:
    """
    GOG از دو منبع:
      ۱. GOG Sales API — بازی‌های با تخفیف بالا
      ۲. GOG Free Games — بازی‌های رایگان موقت
    """
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
    """بازی‌های با تخفیف ≥90% از GOG catalog API"""
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

            games.append(make_game(
                "gog", game_id, title, discount,
                link, orig_fmt, final_fmt,
                image_url=cover,
                is_free_to_keep=is_ftk,
            ))
            if is_ftk:
                log.info(f"  🟣 GOG Free: {title}")
            else:
                log.info(f"  🟣 GOG Sale: {title} -{discount}%")

    except Exception as e:
        log.error(f"GOG sales parse error: {e}")
    return games

def _gog_fetch_free() -> list[dict]:
    """
    GOG Free Games از صفحه گیوِ‌اوِی GOG.
    GOG گاهی یه بازی رو برای چند روز رایگان میکنه.
    """
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
        # کارت‌های بازی
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

                # تصویر
                img_el    = card.select_one("img")
                image_url = img_el.get("src", "") if img_el else ""

                # چک قیمت صفر
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
    """تاریخ پایان تخفیف از صفحه GOG"""
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
#  CAPTION BUILDER (multi-store)
# ═══════════════════════════════════════════════════
def build_caption(game: dict, end_date: str) -> str:
    store    = game["store"]
    meta     = STORE_META[store]
    is_ftk   = game.get("is_free_to_keep", False)
    discount = game["discount"]
    title    = game["title"]

    # توضیحات
    raw_desc = game.get("description") or "No description available."
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc     = raw_desc[:260].rstrip() + ("…" if len(raw_desc) > 260 else "")

    # نقدها (Steam)
    rev_pct   = game.get("review_pct")
    rev_count = game.get("review_count")
    rev_desc  = game.get("review_desc", "")
    if rev_pct is not None and rev_count:
        mood        = "🟢" if rev_pct >= 80 else ("🟡" if rev_pct >= 60 else "🔴")
        review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} reviews — {rev_desc}"
    else:
        review_line = None

    # ژانر (Steam)
    genres    = game.get("genres") or []
    genre_str = ", ".join(genres) if genres else None

    # قیمت
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

    # Hashtags
    tags = ["#FreeGamesHub", meta["tag"]]
    for g in genres[:2]:
        tag = re.sub(r'[^a-zA-Z0-9]', '', g)
        if tag:
            tags.append(f"#{tag}")
    if is_ftk:
        tags.append("#FreeToKeep")
    elif discount == 100:
        tags.append("#FreeGames")
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
        lines += [f"⭐ <b>Steam Reviews:</b> {review_line}", ""]

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

    # محدودیت ۱۰۲۴ تلگرام
    if len(caption) > 1024:
        short = raw_desc[:80].rstrip() + "…"
        new_lines = []
        for line in lines:
            new_lines.append(short if line == desc else line)
        caption = "\n".join(new_lines)

    if len(caption) > 1024:
        # حذف About
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
    store    = game["store"]
    appid    = game["id"]
    url      = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    # لیست تصاویر بر اساس فروشگاه
    if store == "steam":
        candidates = [
            game.get("image_url", ""),
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
            f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
        ]
    else:
        candidates = [game.get("image_url", "")]

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

    # اگه تصویر نداشت، بدون عکس ارسال کن
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
    """
    برمی‌گردونه: ("sent" | "skipped" | "invalid" | "failed", reason)
    """
    store  = game["store"]
    gid    = game["id"]
    title  = game["title"]
    is_ftk = game.get("is_free_to_keep", False)

    # ── Steam: جزئیات اضافه ──────────────────────────────────
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

        # آپدیت قیمت از details
        po = details.get("price_overview") or {}
        if po:
            game["price_orig_fmt"]  = po.get("initial_formatted", game.get("price_orig_fmt", ""))
            game["price_final_fmt"] = po.get("final_formatted",   game.get("price_final_fmt", ""))
            game["discount"]        = po.get("discount_percent",  game["discount"])

        # توضیحات + ژانر + نقدها
        raw = details.get("short_description", "")
        game["description"] = BeautifulSoup(raw, "html.parser").get_text() if raw else ""
        game["genres"]      = [g["description"] for g in details.get("genres", [])]

        rev_pct, rev_count, rev_desc = steam_get_reviews(gid)
        game["review_pct"]   = rev_pct
        game["review_count"] = rev_count
        game["review_desc"]  = rev_desc

        end_display, period_anchor = steam_get_promo_info(gid, is_ftk)

    elif store == "epic":
        end_display, period_anchor = epic_get_promo_info(game)

    elif store == "gog":
        end_display, period_anchor = gog_get_promo_info(game)

    else:
        return "failed", f"unknown store {store}"

    # ── Dedup ────────────────────────────────────────────────
    promo_key = make_promo_key(store, gid, period_anchor)
    log.info(f"       anchor={period_anchor}  key={promo_key}")

    if is_sent(store, gid, promo_key):
        return "skipped", "already sent this period"

    # ── Caption + Send ───────────────────────────────────────
    caption = build_caption(game, end_display)
    ok      = send_game(game, caption)

    if ok:
        mark_sent(store, gid, title, promo_key)
        return "sent", ""
    else:
        return "failed", "telegram send error"

# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
def main():
    log.info("═" * 65)
    log.info("  🎮 FreeGamesHub — Steam + Epic + GOG")
    log.info("═" * 65)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    init_db()

    all_games = []

    # جمع‌آوری از همه فروشگاه‌ها
    log.info("── Fetching Steam ──────────────────────────────────────")
    steam_games = fetch_steam_games()
    _merge(all_games, steam_games)

    log.info("── Fetching Epic ───────────────────────────────────────")
    epic_games = fetch_epic_games()
    _merge(all_games, epic_games)

    log.info("── Fetching GOG ────────────────────────────────────────")
    gog_games = fetch_gog_games()
    _merge(all_games, gog_games)

    log.info(f"Total unique deals: {len(all_games)}")

    if not all_games:
        log.warning("No games found — exiting")
        return

    # مرتب‌سازی: FTK اول، سپس ۱۰۰٪، سپس بیشترین تخفیف
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


if __name__ == "__main__":
    main()
