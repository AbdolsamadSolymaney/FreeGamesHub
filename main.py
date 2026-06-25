"""
FreeGamesHub — Multi-Source Steam Deal & Free Promotions Bot
"""

import os
import time
import logging
import requests
import sqlite3
import datetime
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
MIN_DISCOUNT = 90          # حداقل تخفیف برای ارسال (به جز ۱۰۰٪ که همیشه ارسال می‌شود)

# ایجاد session برای cloudscraper (مقاوم در برابر Cloudflare)
SCRAPER = cloudscraper.create_scraper()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://steamdb.info/",
    "Cookie": (
        "birthtime=0; lastagecheckage=1-January-1990; "
        "wants_mature_content=1; cc=US;"
    ),
}

SKIP_KEYWORDS = [
    "DLC", "Soundtrack", "OST", "Season Pass",
    "Expansion", "Upgrade", "Add-on", "Artbook",
    "Comic", "Deluxe", "Bundle", "Content Pack",
    "Cosmetic", "Starter Pack",
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
#  HTTP HELPER (با پشتیبانی از cloudscraper)
# ═══════════════════════════════════════════════════
def safe_get(url, params=None, retries=3, delay=2, use_scraper=True) -> requests.Response | None:
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
        except requests.exceptions.Timeout:
            log.warning(f"Timeout #{attempt+1} → {url}")
        except Exception as e:
            log.warning(f"Request error #{attempt+1}: {e}")
        time.sleep(delay * (attempt + 1))
    log.error(f"All {retries} attempts failed for {url}")
    return None

# ═══════════════════════════════════════════════════
#  SOURCE 1 & 2: STEAM FEATURED + SEARCH (با پشتیبانی از ۱۰۰٪)
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
    
    # منبع ۳: SteamDB Upcoming Free
    free_db1 = _fetch_steamdb_free("upcoming")
    before = len(games)
    _merge(games, free_db1)
    log.info(f"Source 3 (SteamDB Upcoming Free): {len(free_db1)} → {len(games)-before} new")
    
    # منبع ۴: SteamDB Free (لیست رایگان‌های فعلی)
    free_db2 = _fetch_steamdb_free("current")
    before = len(games)
    _merge(games, free_db2)
    log.info(f"Source 4 (SteamDB Current Free): {len(free_db2)} → {len(games)-before} new")
    
    # منبع ۵: Steam Search با فیلتر free (بازی‌های رایگان استیم)
    steam_free = _fetch_steam_free_search()
    before = len(games)
    _merge(games, steam_free)
    log.info(f"Source 5 (Steam Free Search): {len(steam_free)} → {len(games)-before} new")
    
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
        # specials
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

# ═══════════════════════════════════════════════════
#  SOURCE 3 & 4: STEAMDB (upcoming & current free)
# ═══════════════════════════════════════════════════
def _fetch_steamdb_free(mode: str) -> list[dict]:
    """
    mode: 'upcoming' -> /upcoming/free/
          'current'  -> /free/
    """
    games = []
    if mode == "upcoming":
        url = "https://steamdb.info/upcoming/free/"
    else:
        url = "https://steamdb.info/free/"
    
    r = safe_get(url, retries=3, delay=2, use_scraper=True)
    if not r:
        log.warning(f"SteamDB {mode} free page not accessible.")
        return games
    
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.select_one("table.table")
        if not table:
            log.warning(f"Table not found in {url}")
            return games
        
        rows = table.select("tbody tr")
        for row in rows:
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
                games.append(_make_game(
                    appid, title, 100,
                    orig_fmt="",
                    final_fmt="FREE",
                ))
            except Exception as e:
                log.debug(f"SteamDB {mode} row parse error: {e}")
        
        log.info(f"SteamDB {mode} scraped {len(games)} games")
    except Exception as e:
        log.error(f"SteamDB {mode} scrape error: {e}")
    
    return games

# ═══════════════════════════════════════════════════
#  SOURCE 5: STEAM SEARCH WITH FILTER=FREE
# ═══════════════════════════════════════════════════
def _fetch_steam_free_search() -> list[dict]:
    """
    Scrape Steam search with filter=free to get all free games (including temporary).
    """
    games = []
    r = safe_get(
        "https://store.steampowered.com/search/",
        params={"filter": "free", "category1": "998", "cc": "US", "l": "english"},
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
                # این صفحه معمولاً تخفیف‌ها را نشان نمی‌دهد، اما همه رایگان هستند
                # ما با discount=100 اضافه می‌کنیم
                games.append(_make_game(
                    appid, title, 100,
                    orig_fmt="",
                    final_fmt="FREE",
                ))
            except Exception as e:
                log.debug(f"Steam free search row parse error: {e}")
        log.info(f"Steam free search scraped {len(games)} games")
    except Exception as e:
        log.error(f"Steam free search error: {e}")
    
    return games

# ═══════════════════════════════════════════════════
#  STEAM APP DETAILS, REVIEWS, VALIDATION, CAPTION, SENDER (بدون تغییر)
# ═══════════════════════════════════════════════════
def get_details(appid: str) -> dict | None:
    time.sleep(1.5)
    r = safe_get(
        f"https://store.steampowered.com/api/appdetails",
        params={"appids": appid, "cc": "us", "l": "english"},
        use_scraper=False,
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
        log.error(f"AppDetails parse error ({appid}): {e}")
        return None

def get_steam_reviews(appid: str) -> tuple[int | None, int | None, str]:
    r = safe_get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
        use_scraper=False,
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
        pct = round(pos / total * 100)
        return pct, total, desc
    except Exception as e:
        log.debug(f"Reviews error ({appid}): {e}")
        return None, None, ""

def is_valid(game: dict, details: dict | None) -> bool:
    # همیشه ۱۰۰٪ را قبول کن
    if game["discount"] == 100:
        return True
    if details and details.get("is_free"):
        return True
    if game["discount"] >= MIN_DISCOUNT:
        return True
    return False

def build_caption(game: dict, details: dict | None,
                  rev_pct: int | None, rev_count: int | None,
                  rev_desc: str) -> str:
    details = details or {}
    raw_genres = details.get("genres", [])
    genre_list = [g["description"] for g in raw_genres] if raw_genres else []
    genre_str  = ", ".join(genre_list) if genre_list else "Unknown"

    raw_desc = details.get("short_description", "No description available.")
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc = raw_desc[:280].rstrip()
    if len(raw_desc) > 280:
        desc += "…"

    if rev_pct is not None and rev_count:
        if rev_pct >= 80:
            mood = "🟢"
        elif rev_pct >= 60:
            mood = "🟡"
        else:
            mood = "🔴"
        review_line = f"{mood} <b>{rev_pct}%</b> from {rev_count:,} reviews — {rev_desc}"
    else:
        review_line = "—"

    meta       = details.get("metacritic", {}) or {}
    meta_score = meta.get("score")

    is_free = details.get("is_free", False)
    po = details.get("price_overview") or {}
    if po:
        game["price_original_fmt"] = po.get("initial_formatted", game.get("price_original_fmt", ""))
        game["price_final_fmt"]    = po.get("final_formatted",   game.get("price_final_fmt", ""))
        game["discount"]           = po.get("discount_percent",  game["discount"])

    orig  = game.get("price_original_fmt") or ""
    final = game.get("price_final_fmt")    or ""

    if is_free:
        price_block = "🆓 <b>Free to Play</b>"
        disc_block  = "100% Free 🎁"
    elif game["discount"] == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block  = "100% OFF 🎁 <b>Now free!</b>"
    else:
        price_block = (f"<s>{orig}</s> → <b>{final}</b>"
                       if orig and final else (final or orig or "?"))
        disc_block  = f"<b>-{game['discount']}%</b> 🔥"

    tags = ["#FreeGamesHub", "#SteamDeals"]
    for g in genre_list[:2]:
        tag = g.replace(" ", "").replace("-", "").replace("&", "and")
        tags.append(f"#{tag}")
    if is_free or game["discount"] == 100:
        tags.append("#FreeGames")
    if game["discount"] >= 90:
        tags.append("#MegaDeal")
    hashtags = " ".join(tags)

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

def send_game(game: dict, caption: str) -> bool:
    appid = game["id"]
    image_candidates = [
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg",
    ]

    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    for img_url in image_candidates:
        try:
            r = requests.post(
                api_url,
                data={
                    "chat_id":    CHANNEL,
                    "photo":      img_url,
                    "caption":    caption,
                    "parse_mode": "HTML",
                },
                timeout=30,
            )
            result = r.json()
            if result.get("ok"):
                return True
            err = result.get("description", "")
            log.warning(f"Telegram error: {err}")
            if "can't parse" in err.lower():
                clean_cap = BeautifulSoup(caption, "html.parser").get_text()
                r2 = requests.post(api_url, data={
                    "chat_id": CHANNEL, "photo": img_url,
                    "caption": clean_cap[:1024],
                }, timeout=30)
                if r2.json().get("ok"):
                    return True
            if "wrong type" in err.lower() or "failed" in err.lower():
                continue
        except Exception as e:
            log.error(f"send_game exception: {e}")
    return False

# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
def main():
    log.info("═" * 55)
    log.info("  🎮 FreeGamesHub — Multi-Source Free & Deal Bot")
    log.info("═" * 55)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL not set")
        return

    init_db()

    games = fetch_games()
    if not games:
        log.error("No games found!")
        return

    # مرتب‌سازی: اول ۱۰۰٪، سپس بیشترین تخفیف
    games.sort(key=lambda x: (x["discount"] == 100, x["discount"]), reverse=True)

    sent_ok  = 0
    skipped  = 0
    no_valid = 0
    failed   = 0

    for idx, game in enumerate(games, 1):
        title    = game["title"]
        disc     = game["discount"]
        game_id  = game["id"]

        log.info(f"[{idx:3}/{len(games)}] {title[:50]:<50} | -{disc}%")

        if is_sent(game_id):
            log.info("       ↪ Already sent")
            skipped += 1
            continue

        details = get_details(game_id)
        if not details:
            log.warning(f"       ✗ Details not fetched")
            failed += 1
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

        rev_pct, rev_count, rev_desc = get_steam_reviews(game_id)

        caption = build_caption(game, details, rev_pct, rev_count, rev_desc)

        ok = send_game(game, caption)
        if ok:
            mark_sent(game_id, title)
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
