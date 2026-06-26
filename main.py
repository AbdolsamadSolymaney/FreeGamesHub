"""
FreeGamesHub — Steam Deals + Free to Keep
==========================================
منطق dedup:
  - کلید = hash(game_id + تاریخ_پایان_تخفیف)
  - اگه تاریخ پایان مشخص باشه → از اون استفاده میشه
  - اگه نامشخص باشه → از شماره هفته جاری (YYYY-WW) استفاده میشه
    → هر هفته کلید جدید → تخفیف جدید در هفته‌های بعد ارسال میشه
    → در طول همون هفته تکرار نمیشه
  - Free to Keep → تاریخ انقضا از صفحه Steam
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

# ═══════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            game_id    TEXT,
            promo_key  TEXT,
            title      TEXT,
            sent_at    TEXT,
            PRIMARY KEY (game_id, promo_key)
        )
    """)
    conn.commit()
    conn.close()

def is_sent(game_id: str, promo_key: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute(
        "SELECT 1 FROM sent WHERE game_id=? AND promo_key=?",
        (game_id, promo_key)
    ).fetchone()
    conn.close()
    return row is not None

def mark_sent(game_id: str, title: str, promo_key: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT OR IGNORE INTO sent (game_id, promo_key, title, sent_at) VALUES (?,?,?,?)",
        (game_id, promo_key, title, ts)
    )
    conn.commit()
    conn.close()

def make_promo_key(game_id: str, period_anchor: str) -> str:
    """
    کلید dedup بر اساس game_id + بازه زمانی تخفیف.

    period_anchor چیه:
      - اگه تاریخ پایان تخفیف مشخصه  → "END:2025-08-15"
      - اگه Free to Keep با تاریخ     → "FTK:2025-07-01"
      - اگه تاریخ نامشخصه             → "WEEK:2025-W26"
        (هر هفته کلید جدید میسازه)

    نتیجه:
      ✅ همون هفته دوبار ران بشه → skip
      ✅ هفته بعد ران بشه + بازی هنوز تخفیف داره → skip (چون WEEK تغییر کرده)
         [این رفتار مطلوبه چون تخفیف هنوز همونه]
      ✅ تخفیف تموم شه و ماه بعد دوباره بیاد → WEEK جدید → ارسال میشه ✓
      ✅ تاریخ پایان مشخص باشه → دقیقاً یه بار per پریود ارسال میشه ✓
    """
    raw = f"{game_id}|{period_anchor}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]

def current_week_anchor() -> str:
    """شماره هفته ISO جاری: مثلاً WEEK:2025-W26"""
    now = datetime.datetime.utcnow()
    iso = now.isocalendar()
    return f"WEEK:{iso[0]}-W{iso[1]:02d}"

# ═══════════════════════════════════════════════════
#  HTTP HELPER
# ═══════════════════════════════════════════════════
def safe_get(url, params=None, retries=3, delay=2, use_scraper=False):
    for attempt in range(retries):
        try:
            if use_scraper:
                r = SCRAPER.get(url, params=params, headers=HEADERS, timeout=30)
            else:
                r = requests.get(url, params=params, headers=HEADERS, timeout=20)
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
#  HELPERS
# ═══════════════════════════════════════════════════
def _should_skip(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in SKIP_KEYWORDS)

def _make_game(appid: str, title: str, discount: int,
               orig_fmt: str = "", final_fmt: str = "",
               orig_raw: float = 0, final_raw: float = 0,
               is_free_to_keep: bool = False) -> dict:
    return {
        "id":                  str(appid),
        "title":               title,
        "link":                f"https://store.steampowered.com/app/{appid}/",
        "discount":            discount,
        "price_original_fmt":  orig_fmt,
        "price_final_fmt":     final_fmt,
        "price_original_raw":  orig_raw,
        "price_final_raw":     final_raw,
        "is_free_to_keep":     is_free_to_keep,
    }

def _merge(base: list, new_items: list):
    existing_ids = {g["id"] for g in base}
    for g in new_items:
        if g["id"] not in existing_ids:
            base.append(g)
            existing_ids.add(g["id"])

# ═══════════════════════════════════════════════════
#  SOURCE 1: Featured API
# ═══════════════════════════════════════════════════
def _fetch_featured() -> list[dict]:
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
            games.append(_make_game(
                appid, name, discount,
                f"${orig/100:.2f}" if orig else "",
                f"${final/100:.2f}" if final else "",
                orig / 100, final / 100,
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
                games.append(_make_game(
                    appid, name, disc,
                    f"${orig/100:.2f}" if orig else "",
                    f"${final/100:.2f}" if final else "",
                    orig / 100, final / 100,
                ))
    except Exception as e:
        log.error(f"Featured API parse error: {e}")
    return games

# ═══════════════════════════════════════════════════
#  SOURCE 2: HTML Search (specials ≥90%)
# ═══════════════════════════════════════════════════
def _fetch_html_search() -> list[dict]:
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
                games.append(_make_game(
                    appid, title, discount,
                    orig_el.text.strip() if orig_el else "",
                    final_el.text.strip() if final_el else "",
                ))
            except Exception as e:
                log.debug(f"HTML row parse error: {e}")
    except Exception as e:
        log.error(f"HTML search parse error: {e}")
    return games

# ═══════════════════════════════════════════════════
#  SOURCE 3: Free to Keep
# ═══════════════════════════════════════════════════
def _fetch_free_to_keep() -> list[dict]:
    """
    بازی‌هایی که موقتاً رایگانند (Free to Keep) — نه همیشه رایگان.
    روش اول: Steam search JSON با maxprice=free
    روش دوم (fallback): HTML همون صفحه
    """
    games = []

    # روش اول: JSON
    r = safe_get(
        "https://store.steampowered.com/search/results/",
        params={
            "specials": 1,
            "maxprice": "free",
            "cc":       "US",
            "l":        "english",
            "json":     1,
            "count":    50,
        },
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
                games.append(_make_game(
                    appid, title, 100,
                    orig_fmt="", final_fmt="FREE",
                    is_free_to_keep=True,
                ))
                log.info(f"  🎁 Free to Keep (JSON): {title} ({appid})")
        except Exception as e:
            log.error(f"Free to Keep JSON parse error: {e}")

    # روش دوم: HTML fallback
    if not games:
        log.info("Free to Keep JSON empty — trying HTML fallback")
        r2 = safe_get(
            "https://store.steampowered.com/search/",
            params={
                "specials": 1,
                "maxprice": "free",
                "cc":       "US",
                "l":        "english",
            },
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
                    if not disc_el:
                        continue
                    if "-100%" not in disc_el.text:
                        continue
                    games.append(_make_game(
                        appid, title, 100,
                        orig_fmt="", final_fmt="FREE",
                        is_free_to_keep=True,
                    ))
                    log.info(f"  🎁 Free to Keep (HTML): {title} ({appid})")
            except Exception as e:
                log.error(f"Free to Keep HTML parse error: {e}")

    log.info(f"Free to Keep total: {len(games)} games")
    return games

# ═══════════════════════════════════════════════════
#  AGGREGATE
# ═══════════════════════════════════════════════════
def fetch_games() -> list[dict]:
    games = []

    featured = _fetch_featured()
    _merge(games, featured)
    log.info(f"Source 1 (Featured API):  {len(featured)} games")

    html_games = _fetch_html_search()
    before = len(games)
    _merge(games, html_games)
    log.info(f"Source 2 (HTML Search):   {len(html_games)} raw → {len(games)-before} new")

    free_games = _fetch_free_to_keep()
    before = len(games)
    _merge(games, free_games)
    log.info(f"Source 3 (Free to Keep):  {len(free_games)} raw → {len(games)-before} new")

    log.info(f"Total unique deals: {len(games)}")
    return games

# ═══════════════════════════════════════════════════
#  STEAM APP DETAILS
# ═══════════════════════════════════════════════════
def get_details(appid: str) -> dict | None:
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
        log.error(f"AppDetails error ({appid}): {e}")
        return None

# ═══════════════════════════════════════════════════
#  DISCOUNT DATES — از صفحه HTML استیم
# ═══════════════════════════════════════════════════
def get_promo_info(appid: str, is_ftk: bool) -> tuple[str, str]:
    """
    برمی‌گردونه: (end_date_display, period_anchor)

    end_date_display: برای نمایش در caption (مثلاً "2025-08-15")
    period_anchor:    برای ساخت promo_key (مثلاً "END:2025-08-15")

    منطق:
      ۱. صفحه بازی رو می‌خونه
      ۲. دنبال "Offer ends DATE" می‌گرده
      ۳. اگه پیدا شد → از تاریخ پایان استفاده میکنه (دقیق‌ترین حالت)
      ۴. اگه پیدا نشد:
           - Free to Keep → "FTK:WEEK:YYYY-WW" (هر هفته یه بار)
           - تخفیف عادی  → "WEEK:YYYY-WW"     (هر هفته یه بار)
    """
    r = safe_get(
        f"https://store.steampowered.com/app/{appid}/",
        retries=2, delay=1,
    )

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
                        end_dt       = datetime.datetime.strptime(end_str, "%b %d, %Y")
                        end_display  = end_dt.strftime("%Y-%m-%d")
                        anchor_type  = "FTK" if is_ftk else "END"
                        period_anchor = f"{anchor_type}:{end_display}"
                        return end_display, period_anchor
                    except ValueError:
                        pass

            # جستجوی جایگزین: game_area_purchase_game
            purchase = soup.select_one(".game_area_purchase_game")
            if purchase:
                text  = purchase.get_text(separator=" ", strip=True)
                match = re.search(
                    r"(?:ends?|until|expires?)\s+([\w]+\s+\d{1,2},?\s+\d{4})",
                    text, re.IGNORECASE
                )
                if match:
                    end_str = match.group(1).replace(",", "")
                    for fmt in ("%b %d %Y", "%B %d %Y"):
                        try:
                            end_dt       = datetime.datetime.strptime(end_str, fmt)
                            end_display  = end_dt.strftime("%Y-%m-%d")
                            anchor_type  = "FTK" if is_ftk else "END"
                            period_anchor = f"{anchor_type}:{end_display}"
                            return end_display, period_anchor
                        except ValueError:
                            continue
        except Exception as e:
            log.debug(f"Promo date extraction failed for {appid}: {e}")

    # Fallback: شماره هفته جاری
    week_anchor = current_week_anchor()
    prefix      = "FTK:" if is_ftk else ""
    return "Unknown", f"{prefix}{week_anchor}"

# ═══════════════════════════════════════════════════
#  REVIEWS
# ═══════════════════════════════════════════════════
def get_steam_reviews(appid: str):
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

# ═══════════════════════════════════════════════════
#  VALIDATION
# ═══════════════════════════════════════════════════
def is_valid(game: dict, details: dict | None) -> bool:
    # Free-to-Play دائمی رد بشه — مگه Free to Keep باشه
    if details and details.get("is_free", False) and not game.get("is_free_to_keep"):
        return False
    if game["discount"] == 100:
        return True
    if game["discount"] >= MIN_DISCOUNT:
        return True
    return False

# ═══════════════════════════════════════════════════
#  CAPTION BUILDER
# ═══════════════════════════════════════════════════
def build_caption(game: dict, details: dict | None,
                  rev_pct: int | None, rev_count: int | None,
                  rev_desc: str, end_date: str) -> str:

    details    = details or {}
    genre_list = [g["description"] for g in details.get("genres", [])] if details.get("genres") else []
    genre_str  = ", ".join(genre_list) if genre_list else "Unknown"

    raw_desc = details.get("short_description", "No description.")
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc     = raw_desc[:280].rstrip() + ("…" if len(raw_desc) > 280 else "")

    if rev_pct is not None and rev_count:
        mood        = "🟢" if rev_pct >= 80 else ("🟡" if rev_pct >= 60 else "🔴")
        review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} reviews — {rev_desc}"
    else:
        review_line = "—"

    meta       = details.get("metacritic", {}) or {}
    meta_score = meta.get("score")

    po = details.get("price_overview") or {}
    if po:
        game["price_original_fmt"] = po.get("initial_formatted", game.get("price_original_fmt", ""))
        game["price_final_fmt"]    = po.get("final_formatted",   game.get("price_final_fmt", ""))
        game["discount"]           = po.get("discount_percent",  game["discount"])

    orig  = game.get("price_original_fmt") or ""
    final = game.get("price_final_fmt") or ""
    is_ftk = game.get("is_free_to_keep", False)

    if game["discount"] == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block  = "100% OFF 🎁 <b>Free to Keep!</b>" if is_ftk else "100% OFF 🎁 <b>Now free!</b>"
    else:
        price_block = (
            f"<s>{orig}</s> → <b>{final}</b>"
            if orig and final else (final or orig or "?")
        )
        disc_block = f"<b>-{game['discount']}%</b> 🔥"

    tags = ["#FreeGamesHub", "#SteamDeals"]
    for g in genre_list[:2]:
        tag = re.sub(r'[^a-zA-Z0-9]', '', g)
        if tag:
            tags.append(f"#{tag}")
    if is_ftk:
        tags.append("#FreeToKeep")
    elif game["discount"] == 100:
        tags.append("#FreeGames")
    if game["discount"] >= 90:
        tags.append("#MegaDeal")
    hashtags = " ".join(tags)

    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    lines = [
        f"🎮 <b>{game['title']}</b>",
        "",
        f"🎯 <b>Genre:</b> {genre_str}",
        "",
        "📝 <b>About:</b>",
        desc,
        "",
        f"⭐ <b>Steam Reviews:</b> {review_line}",
    ]
    if meta_score:
        lines.append(f"🏆 <b>Metacritic:</b> {meta_score}/100")

    lines += [
        "",
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
        short_desc = raw_desc[:100].rstrip() + "…"
        for i, line in enumerate(lines):
            if line == desc:
                lines[i] = short_desc
                break
        caption = "\n".join(lines)

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
    appid      = game["id"]
    candidates = [
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
    ]
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

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
                    "chat_id": CHANNEL,
                    "photo":   img,
                    "caption": clean[:1024],
                }, timeout=30)
                if r2.json().get("ok"):
                    return True
            if "wrong type" in err or "failed" in err:
                continue
        except Exception as e:
            log.error(f"Send exception: {e}")

    return False

# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
def main():
    log.info("═" * 60)
    log.info("  🎮 FreeGamesHub — Steam Deals + Free to Keep")
    log.info("═" * 60)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    init_db()
    games = fetch_games()

    if not games:
        log.warning("No games found — exiting")
        return

    # مرتب‌سازی: اول Free to Keep، سپس ۱۰۰٪، سپس بیشترین تخفیف
    games.sort(key=lambda x: (
        x.get("is_free_to_keep", False),
        x["discount"] == 100,
        x["discount"],
    ), reverse=True)

    sent_ok = skipped = no_valid = failed = 0

    for idx, game in enumerate(games, 1):
        title  = game["title"]
        disc   = game["discount"]
        gid    = game["id"]
        is_ftk = game.get("is_free_to_keep", False)
        label  = "🎁 FTK" if is_ftk else f"-{disc}%"

        log.info(f"[{idx:3}/{len(games)}] {title[:48]:<48} | {label}")

        # ── ۱. جزئیات بازی ─────────────────────────────────────
        details = get_details(gid)
        if not details:
            log.warning("       ↪ No details — skipped")
            failed += 1
            continue

        # ── ۲. رد Free-to-Play دائمی ───────────────────────────
        if details.get("is_free", False) and not is_ftk:
            log.info("       ↪ Free-to-Play (permanent) — skipped")
            no_valid += 1
            continue

        # ── ۳. رد انواع غیر بازی ───────────────────────────────
        app_type = details.get("type", "")
        if app_type not in ("game", ""):
            log.info(f"       ↪ Type {app_type!r} — skipped")
            no_valid += 1
            continue

        # ── ۴. اعتبارسنجی تخفیف ────────────────────────────────
        if not is_valid(game, details):
            log.info(f"       ↪ Discount {disc}% below threshold — skipped")
            no_valid += 1
            continue

        # ── ۵. تاریخ پایان + anchor برای dedup ─────────────────
        end_display, period_anchor = get_promo_info(gid, is_ftk)
        promo_key = make_promo_key(gid, period_anchor)

        log.info(f"       anchor={period_anchor}  key={promo_key}")

        # ── ۶. چک تکراری بودن ──────────────────────────────────
        if is_sent(gid, promo_key):
            log.info("       ↪ Already sent for this promo period — skipped")
            skipped += 1
            continue

        # ── ۷. نقدها ────────────────────────────────────────────
        rev_pct, rev_count, rev_desc = get_steam_reviews(gid)

        # ── ۸. Caption ──────────────────────────────────────────
        caption = build_caption(game, details, rev_pct, rev_count, rev_desc, end_display)

        # ── ۹. ارسال ────────────────────────────────────────────
        ok = send_game(game, caption)
        if ok:
            mark_sent(gid, title, promo_key)
            sent_ok += 1
            log.info("       ✅ Sent")
        else:
            failed += 1
            log.error("       ❌ Send failed")

        time.sleep(3)

    log.info("═" * 60)
    log.info(f"  ✅ Sent:             {sent_ok}")
    log.info(f"  ⏭  Skipped (dup):   {skipped}")
    log.info(f"  ⚠️  Invalid/skipped: {no_valid}")
    log.info(f"  ❌ Errors:           {failed}")
    log.info("═" * 60)


if __name__ == "__main__":
    main()
