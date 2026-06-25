"""
FreeGamesHub — Steam Deals + Free Promotions with Dates
"""

import os
import time
import logging
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
MIN_DISCOUNT = 90          # حداقل تخفیف برای ارسال (به جز ۱۰۰٪ که همیشه بررسی می‌شود)

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
            id       TEXT PRIMARY KEY,
            title    TEXT,
            sent_at  TEXT
        )
    """)
    conn.commit()
    conn.close()

def is_sent(game_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT 1 FROM sent WHERE id=?", (game_id,)).fetchone()
    conn.close()
    return row is not None

def mark_sent(game_id: str, title: str = ""):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?)", (game_id, title, ts))
    conn.commit()
    conn.close()

# ═══════════════════════════════════════════════════
#  HTTP HELPER
# ═══════════════════════════════════════════════════
def safe_get(url, params=None, retries=3, delay=2, use_scraper=True):
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
#  SOURCES (بدون Steam Free Search)
# ═══════════════════════════════════════════════════
def fetch_games() -> list[dict]:
    games = []

    # منبع ۱: Featured API
    featured = _fetch_featured()
    _merge(games, featured)
    log.info(f"Source 1 (Featured): {len(featured)} games")

    # منبع ۲: HTML Search (specials)
    html_games = _fetch_html_search()
    before = len(games)
    _merge(games, html_games)
    log.info(f"Source 2 (HTML Search): {len(html_games)} raw → {len(games)-before} new")

    # منبع ۳: SteamDB Upcoming Free (اگر دسترسی داشت)
    free_db1 = _fetch_steamdb_free("upcoming")
    before = len(games)
    _merge(games, free_db1)
    log.info(f"Source 3 (SteamDB Upcoming): {len(free_db1)} → {len(games)-before} new")

    # منبع ۴: SteamDB Current Free
    free_db2 = _fetch_steamdb_free("current")
    before = len(games)
    _merge(games, free_db2)
    log.info(f"Source 4 (SteamDB Current): {len(free_db2)} → {len(games)-before} new")

    log.info(f"Total unique deals collected: {len(games)}")
    return games

def _merge(base: list, new_items: list):
    existing_ids = {g["id"] for g in base}
    for g in new_items:
        if g["id"] not in existing_ids:
            base.append(g)
            existing_ids.add(g["id"])

def _should_skip(title: str) -> bool:
    return any(kw.lower() in title.lower() for kw in SKIP_KEYWORDS)

def _make_game(appid: str, title: str, discount: int,
               orig_fmt: str = "", final_fmt: str = "",
               orig_raw: float = 0, final_raw: float = 0) -> dict:
    return {
        "id":                str(appid),
        "title":             title,
        "link":              f"https://store.steampowered.com/app/{appid}/",
        "discount":          discount,
        "price_original_fmt": orig_fmt,
        "price_final_fmt":   final_fmt,
        "price_original_raw": orig_raw,
        "price_final_raw":   final_raw,
    }

def _fetch_featured() -> list[dict]:
    games = []
    r = safe_get("https://store.steampowered.com/api/featuredcategories/",
                 params={"cc": "US", "l": "english"}, use_scraper=False)
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
        # top_sellers و new_releases
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

def _fetch_html_search() -> list[dict]:
    games = []
    r = safe_get(
        "https://store.steampowered.com/search/",
        params={"specials": 1, "cc": "US", "l": "english"},
        use_scraper=False,
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
                disc_el = row.select_one(".discount_pct")
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

def _fetch_steamdb_free(mode: str) -> list[dict]:
    games = []
    url = "https://steamdb.info/upcoming/free/" if mode == "upcoming" else "https://steamdb.info/free/"
    r = safe_get(url, retries=3, delay=2, use_scraper=True)
    if not r:
        return games
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.select_one("table.table")
        if not table:
            return games
        for row in table.select("tbody tr"):
            try:
                link_tag = row.select_one("td a")
                if not link_tag:
                    continue
                href = link_tag.get("href", "")
                if "/app/" not in href:
                    continue
                appid = href.split("/app/")[1].strip("/")
                if not appid.isdigit():
                    continue
                title = link_tag.text.strip()
                if _should_skip(title):
                    continue
                games.append(_make_game(appid, title, 100, orig_fmt="", final_fmt="FREE"))
            except Exception as e:
                log.debug(f"SteamDB {mode} row error: {e}")
        log.info(f"SteamDB {mode} scraped {len(games)} games")
    except Exception as e:
        log.error(f"SteamDB {mode} scrape error: {e}")
    return games

# ═══════════════════════════════════════════════════
#  GET DISCOUNT DATES (با اسکرپ صفحه بازی)
# ═══════════════════════════════════════════════════
def get_discount_dates(appid: str) -> tuple[str, str]:
    """
    استخراج تاریخ شروع و پایان تخفیف از صفحهٔ استیم.
    برمی‌گرداند: (start_date, end_date) به فرمت UTC یا "Unknown"
    """
    url = f"https://store.steampowered.com/app/{appid}/"
    r = safe_get(url, use_scraper=False, retries=2, delay=1)
    if not r:
        return "Unknown", "Unknown"

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        # معمولاً تاریخ تخفیف در المان‌های class="game_purchase_discount_quantity" یا مشابه
        # یا در بخش "Special Promotion" با کلاس "discount_block"
        discount_block = soup.select_one(".discount_block")
        if discount_block:
            text = discount_block.get_text(separator=" ", strip=True)
            # جستجوی الگوی تاریخ مثل "Offer ends 25 Jun, 2026" یا "Ends in ..."
            match = re.search(r"Offer ends\s+([\w]+\s+\d{1,2},\s+\d{4})", text, re.IGNORECASE)
            if match:
                end_str = match.group(1)
                # تبدیل به فرمت استاندارد (تلاش برای parse)
                try:
                    end_dt = datetime.datetime.strptime(end_str, "%b %d, %Y")
                    end_utc = end_dt.strftime("%Y-%m-%d %H:%M UTC")
                except:
                    end_utc = end_str
                # تاریخ شروع معمولاً درج نمی‌شود، از امروز استفاده می‌کنیم
                start_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
                return start_utc, end_utc
        # اگر پیدا نشد، از المنت‌های دیگر استفاده کنیم
        # گاهی در "game_purchase_price" یا "discount_pct" اطلاعاتی هست
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "Unknown"
    except Exception as e:
        log.debug(f"Date extraction failed for {appid}: {e}")
        return "Unknown", "Unknown"

# ═══════════════════════════════════════════════════
#  STEAM APP DETAILS
# ═══════════════════════════════════════════════════
def get_details(appid: str) -> dict | None:
    time.sleep(1.2)
    r = safe_get(
        f"https://store.steampowered.com/api/appdetails",
        params={"appids": appid, "cc": "us", "l": "english"},
        use_scraper=False,
    )
    if not r:
        return None
    try:
        data = r.json()
        app = data.get(str(appid), {})
        if not app.get("success"):
            return None
        return app["data"]
    except Exception as e:
        log.error(f"AppDetails error ({appid}): {e}")
        return None

# ═══════════════════════════════════════════════════
#  REVIEWS
# ═══════════════════════════════════════════════════
def get_steam_reviews(appid: str):
    r = safe_get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
        use_scraper=False,
    )
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

# ═══════════════════════════════════════════════════
#  VALIDATION (رد بازی‌های Free to Play)
# ═══════════════════════════════════════════════════
def is_valid(game: dict, details: dict | None) -> bool:
    # اگر بازی رایگان دائمی باشد، رد کن
    if details and details.get("is_free", False):
        return False
    # تخفیف ۱۰۰٪ (موقت رایگان) – قبول
    if game["discount"] == 100:
        return True
    # تخفیف >= MIN_DISCOUNT
    if game["discount"] >= MIN_DISCOUNT:
        return True
    return False

# ═══════════════════════════════════════════════════
#  CAPTION BUILDER (با تاریخ شروع و پایان)
# ═══════════════════════════════════════════════════
def build_caption(game: dict, details: dict | None,
                  rev_pct: int | None, rev_count: int | None,
                  rev_desc: str, start_date: str, end_date: str) -> str:

    details = details or {}
    genre_list = [g["description"] for g in details.get("genres", [])] if details.get("genres") else []
    genre_str = ", ".join(genre_list) if genre_list else "Unknown"

    raw_desc = details.get("short_description", "No description.")
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc = raw_desc[:280].rstrip() + ("…" if len(raw_desc) > 280 else "")

    # Reviews
    if rev_pct is not None and rev_count:
        mood = "🟢" if rev_pct >= 80 else ("🟡" if rev_pct >= 60 else "🔴")
        review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} reviews — {rev_desc}"
    else:
        review_line = "—"

    meta = details.get("metacritic", {}) or {}
    meta_score = meta.get("score")

    # Price
    po = details.get("price_overview") or {}
    if po:
        game["price_original_fmt"] = po.get("initial_formatted", game.get("price_original_fmt", ""))
        game["price_final_fmt"]    = po.get("final_formatted", game.get("price_final_fmt", ""))
        game["discount"]           = po.get("discount_percent", game["discount"])

    orig  = game.get("price_original_fmt") or ""
    final = game.get("price_final_fmt") or ""

    if game["discount"] == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block  = "100% OFF 🎁 <b>Now free!</b>"
    else:
        price_block = (f"<s>{orig}</s> → <b>{final}</b>" if orig and final else (final or orig or "?"))
        disc_block  = f"<b>-{game['discount']}%</b> 🔥"

    # Hashtags
    tags = ["#FreeGamesHub", "#SteamDeals"]
    for g in genre_list[:2]:
        tag = re.sub(r'[^a-zA-Z0-9]', '', g)
        tags.append(f"#{tag}")
    if game["discount"] == 100:
        tags.append("#FreeGames")
    if game["discount"] >= 90:
        tags.append("#MegaDeal")
    hashtags = " ".join(tags)

    # ساخت کپشن با تاریخ
    lines = [
        f"🎮 <b>{game['title']}</b>",
        "",
        f"🎯 <b>Genre:</b> {genre_str}",
        "",
        f"📝 <b>About:</b>",
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
        f"⏳ <b>Start:</b> {start_date}",
        f"⏳ <b>End:</b>   {end_date}",
        "",
        f"🔗 {game['link']}",
        "",
        hashtags,
    ]

    caption = "\n".join(lines)

    # محدودیت ۱۰۲۴
    if len(caption) > 1024:
        short_desc = raw_desc[:100].rstrip() + "…"
        for i, line in enumerate(lines):
            if line == desc:
                lines[i] = short_desc
                break
        caption = "\n".join(lines)
        if len(caption) > 1024:
            new_lines = []
            skip = False
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

# ═══════════════════════════════════════════════════
#  TELEGRAM SENDER
# ═══════════════════════════════════════════════════
def send_game(game: dict, caption: str) -> bool:
    appid = game["id"]
    candidates = [
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
    ]
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    for img in candidates:
        try:
            r = requests.post(url, data={
                "chat_id": CHANNEL,
                "photo": img,
                "caption": caption,
                "parse_mode": "HTML",
            }, timeout=30)
            if r.json().get("ok"):
                return True
            err = r.json().get("description", "")
            log.warning(f"Telegram error: {err}")
            if "can't parse" in err:
                clean = BeautifulSoup(caption, "html.parser").get_text()
                r2 = requests.post(url, data={"chat_id": CHANNEL, "photo": img, "caption": clean[:1024]})
                if r2.json().get("ok"):
                    return True
            if "wrong type" in err or "failed" in err:
                continue
        except Exception as e:
            log.error(f"send exception: {e}")
    return False

# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
def main():
    log.info("═" * 55)
    log.info("  🎮 FreeGamesHub — Deals + Free Promos with Dates")
    log.info("═" * 55)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    init_db()
    games = fetch_games()
    if not games:
        log.error("No games found!")
        return

    # اولویت با ۱۰۰٪ و سپس بیشترین تخفیف
    games.sort(key=lambda x: (x["discount"] == 100, x["discount"]), reverse=True)

    sent_ok = skipped = no_valid = failed = 0

    for idx, game in enumerate(games, 1):
        title = game["title"]
        disc = game["discount"]
        gid = game["id"]

        log.info(f"[{idx:3}/{len(games)}] {title[:50]:<50} | -{disc}%")

        if is_sent(gid):
            skipped += 1
            continue

        details = get_details(gid)
        if not details:
            failed += 1
            continue

        # رد بازی‌های رایگان دائمی
        if details.get("is_free", False):
            log.info(f"       ↪ Free-to-play — skipped")
            no_valid += 1
            continue

        app_type = details.get("type", "")
        if app_type not in ("game", ""):
            log.info(f"       ↪ Type {app_type!r} — skipped")
            no_valid += 1
            continue

        if not is_valid(game, details):
            log.info(f"       ↪ Insufficient discount ({disc}%) — skipped")
            no_valid += 1
            continue

        # دریافت تاریخ تخفیف
        start_date, end_date = get_discount_dates(gid)

        rev_pct, rev_count, rev_desc = get_steam_reviews(gid)
        caption = build_caption(game, details, rev_pct, rev_count, rev_desc, start_date, end_date)

        ok = send_game(game, caption)
        if ok:
            mark_sent(gid, title)
            sent_ok += 1
            log.info(f"       ✅ Sent")
        else:
            failed += 1
            log.error(f"       ❌ Send failed")

        time.sleep(3)

    log.info("═" * 55)
    log.info(f"  ✅ Sent:          {sent_ok}")
    log.info(f"  ⏭  Skipped (dup): {skipped}")
    log.info(f"  ⚠️  Invalid:      {no_valid}")
    log.info(f"  ❌ Errors:        {failed}")
    log.info("═" * 55)

if __name__ == "__main__":
    main()
