"""
FreeGamesHub — LEGEND MODE
Steam Deal Intelligence Bot for Telegram
"""

import os
import time
import logging
import requests
import sqlite3
import datetime
import jdatetime
from bs4 import BeautifulSoup

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
MIN_DISCOUNT = 90          # حداقل تخفیف برای ارسال

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # bypass age gate & region gate
    "Cookie": (
        "birthtime=0; lastagecheckage=1-January-1990; "
        "wants_mature_content=1; "
        "cc=US; "
    ),
}

# کلمات‌هایی که باید از لیست حذف شوند (DLC، OST، ...)
SKIP_KEYWORDS = [
    "DLC", "Soundtrack", "OST", "Season Pass",
    "Expansion", "Upgrade", "Add-on", "Artbook",
    "Comic", "Deluxe", "Bundle", "Content Pack",
    "Cosmetic", "Starter Pack",
]

# ═══════════════════════════════════════════════════
#  GENRE TRANSLATIONS  (فارسی)
# ═══════════════════════════════════════════════════
GENRE_FA = {
    "Action":                   "اکشن",
    "Adventure":                "ماجراجویی",
    "RPG":                      "نقش‌آفرینی",
    "Strategy":                 "استراتژی",
    "Simulation":               "شبیه‌سازی",
    "Sports":                   "ورزشی",
    "Racing":                   "مسابقه‌ای",
    "Puzzle":                   "پازل",
    "Horror":                   "وحشت",
    "Shooter":                  "تیراندازی",
    "Platformer":               "سکوبازی",
    "Casual":                   "کژوال",
    "Indie":                    "مستقل",
    "Massively Multiplayer":    "چندنفره آنلاین",
    "Free to Play":             "رایگان",
    "Early Access":             "دسترسی زودهنگام",
    "Fighting":                 "مبارزه‌ای",
    "Stealth":                  "مخفی‌کاری",
    "Survival":                 "بقا",
    "Open World":               "جهان باز",
    "Card Game":                "بازی کارتی",
    "Turn-Based Strategy":      "استراتژی نوبتی",
    "Tower Defense":            "دفاع از برج",
    "Visual Novel":             "رمان تصویری",
    "Anime":                    "انیمه",
    "2D":                       "دوبعدی",
    "3D":                       "سه‌بعدی",
    "Hack and Slash":           "شمشیربازی",
    "Sandbox":                  "آزاد",
    "MOBA":                     "موبا",
    "Battle Royale":            "بتل رویال",
    "Point & Click":            "نقطه‌ای",
    "Walking Simulator":        "شبیه‌ساز قدم‌زنی",
    "Education":                "آموزشی",
    "Music":                    "موسیقی",
    "Rhythm":                   "ریتم",
}

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
#  TIME HELPERS
# ═══════════════════════════════════════════════════
def get_times():
    utc    = datetime.datetime.utcnow()
    iran   = utc + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran)
    return utc, iran, shamsi


# ═══════════════════════════════════════════════════
#  HTTP HELPER  (retry + delay)
# ═══════════════════════════════════════════════════
def safe_get(url, params=None, retries=3, delay=2) -> requests.Response | None:
    for attempt in range(retries):
        try:
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
#  STEAM GAME LISTING  (3 kaynak / 3 منبع)
# ═══════════════════════════════════════════════════
def fetch_games() -> list[dict]:
    """
    منابع مختلف را ترکیب می‌کند:
      1. Steam Featured Categories API  (بهترین)
      2. Steam Search HTML scraping     (بیشترین بازی)
    """
    games: list[dict] = []

    # ── منبع ۱: Featured Categories (مستقیم JSON) ──────────────
    featured = _fetch_featured()
    _merge(games, featured)
    log.info(f"Source 1 (Featured): {len(featured)} games")

    # ── منبع ۲: HTML Search scraping ────────────────────────────
    html_games = _fetch_html_search()
    before = len(games)
    _merge(games, html_games)
    log.info(f"Source 2 (HTML Search): {len(html_games)} raw → {len(games)-before} new added")

    log.info(f"Total unique deals collected: {len(games)}")
    return games


def _merge(base: list, new_items: list):
    """جلوگیری از تکرار با مقایسه ID"""
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
                 params={"cc": "US", "l": "english"})
    if not r:
        return games

    try:
        data = r.json()

        # specials bölümü / بخش تخفیف‌های ویژه
        for item in data.get("specials", {}).get("items", []):
            name = item.get("name", "")
            if not name or _should_skip(name):
                continue

            appid    = str(item.get("id", ""))
            discount = item.get("discount_percent", 0)
            orig     = item.get("original_price", 0)   # cents
            final    = item.get("final_price", 0)

            if not appid:
                continue

            games.append(_make_game(
                appid, name, discount,
                f"${orig/100:.2f}" if orig else "",
                f"${final/100:.2f}" if final else "",
                orig / 100, final / 100,
            ))

        # coming_soon, top_sellers vb.'de de indirim olabilir
        for section_key in ["top_sellers", "new_releases"]:
            for item in data.get(section_key, {}).get("items", []):
                disc = item.get("discount_percent", 0)
                if disc < MIN_DISCOUNT:
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
    """HTML scrape از صفحه جستجوی Steam"""
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

                # تخفیف
                disc_el = row.select_one(".discount_pct")
                discount = 0
                if disc_el:
                    try:
                        discount = int(
                            disc_el.text.strip().replace("-", "").replace("%", "")
                        )
                    except ValueError:
                        pass

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
#  STEAM APP DETAILS
# ═══════════════════════════════════════════════════
def get_details(appid: str) -> dict | None:
    time.sleep(1.5)   # rate limiting — مهم!
    r = safe_get(
        f"https://store.steampowered.com/api/appdetails",
        params={"appids": appid, "cc": "us", "l": "english"},
    )
    if not r:
        return None

    try:
        data = r.json()
        app  = data.get(str(appid), {})
        if not app.get("success"):
            log.debug(f"AppDetails not successful for {appid}")
            return None
        return app["data"]
    except Exception as e:
        log.error(f"AppDetails parse error ({appid}): {e}")
        return None


# ═══════════════════════════════════════════════════
#  STEAM REVIEWS
# ═══════════════════════════════════════════════════
def get_steam_reviews(appid: str) -> tuple[int | None, int | None, str]:
    """درصد مثبت، تعداد کل، توضیح نمره"""
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

        pct = round(pos / total * 100)
        return pct, total, desc

    except Exception as e:
        log.debug(f"Reviews error ({appid}): {e}")
        return None, None, ""


# ═══════════════════════════════════════════════════
#  VALIDATION
# ═══════════════════════════════════════════════════
def is_valid(game: dict, details: dict | None) -> bool:
    # رایگان همیشه
    if details and details.get("is_free"):
        return True
    # ۱۰۰٪ تخفیف = رایگان موقت
    if game["discount"] == 100:
        return True
    # حداقل تخفیف
    if game["discount"] >= MIN_DISCOUNT:
        return True
    return False


# ═══════════════════════════════════════════════════
#  CAPTION BUILDER
# ═══════════════════════════════════════════════════
def build_caption(game: dict, details: dict | None,
                  rev_pct: int | None, rev_count: int | None,
                  rev_desc: str) -> str:

    utc, iran, shamsi = get_times()
    details = details or {}

    # ── ژانر ───────────────────────────────────────
    raw_genres    = details.get("genres", [])
    genre_en_list = [g["description"] for g in raw_genres] if raw_genres else []
    genre_fa_list = [GENRE_FA.get(g, g) for g in genre_en_list]
    genre_en = ", ".join(genre_en_list) if genre_en_list else "Unknown"
    genre_fa = "، ".join(genre_fa_list) if genre_fa_list else "نامشخص"

    # ── توضیحات ────────────────────────────────────
    raw_desc = details.get("short_description", "No description available.")
    # HTML tags را پاک می‌کنیم
    raw_desc = BeautifulSoup(raw_desc, "html.parser").get_text()
    desc     = raw_desc[:280].rstrip()
    if len(raw_desc) > 280:
        desc += "…"

    # ── نظرات Steam ────────────────────────────────
    if rev_pct is not None and rev_count:
        if rev_pct >= 80:
            mood = "🟢"
        elif rev_pct >= 60:
            mood = "🟡"
        else:
            mood = "🔴"
        review_line = f"{mood} <b>{rev_pct}%</b> از {rev_count:,} نظر — {rev_desc}"
    else:
        review_line = "—"

    # ── Metacritic ──────────────────────────────────
    meta       = details.get("metacritic", {}) or {}
    meta_score = meta.get("score")

    # ── قیمت و تخفیف ───────────────────────────────
    is_free = details.get("is_free", False)

    # مقادیر دقیق‌تر را از API می‌گیریم
    po = details.get("price_overview") or {}
    if po:
        game["price_original_fmt"] = po.get("initial_formatted", game.get("price_original_fmt", ""))
        game["price_final_fmt"]    = po.get("final_formatted",   game.get("price_final_fmt", ""))
        game["discount"]           = po.get("discount_percent",  game["discount"])

    orig  = game.get("price_original_fmt") or ""
    final = game.get("price_final_fmt")    or ""

    if is_free:
        price_block = "🆓 <b>Free to Play</b>"
        disc_block  = "100% رایگان 🎁"
    elif game["discount"] == 100:
        price_block = f"<s>{orig}</s> → <b>FREE</b>" if orig else "<b>FREE</b>"
        disc_block  = "100% OFF 🎁 <b>الان رایگانه!</b>"
    else:
        price_block = (f"<s>{orig}</s> → <b>{final}</b>"
                       if orig and final else (final or orig or "?"))
        disc_block  = f"<b>-{game['discount']}%</b> 🔥"

    # ── هشتگ ───────────────────────────────────────
    tags = ["#FreeGamesHub", "#SteamDeals"]
    for g in genre_en_list[:2]:
        tag = g.replace(" ", "").replace("-", "").replace("&", "and")
        tags.append(f"#{tag}")
    if is_free or game["discount"] == 100:
        tags.append("#FreeGames")
    if game["discount"] >= 90:
        tags.append("#MegaDeal")
    hashtags = " ".join(tags)

    # ── متن نهایی ──────────────────────────────────
    lines = [
        f"🎮 <b>{game['title']}</b>",
        "",
        f"🎯 <b>Genre:</b> {genre_en}",
        f"🎮 <b>ژانر:</b> {genre_fa}",
        "",
        f"📝 <b>درباره بازی:</b>",
        desc,
        "",
        f"⭐ <b>نظرات Steam:</b> {review_line}",
    ]

    if meta_score:
        lines.append(f"🏆 <b>Metacritic:</b> {meta_score}/100")

    lines += [
        "",
        f"💰 <b>قیمت:</b> {price_block}",
        f"💸 <b>تخفیف:</b> {disc_block}",
        "",
        f"📅 <b>تاریخ شمسی:</b> {shamsi.strftime('%Y/%m/%d')} ⏰ {iran.strftime('%H:%M')}",
        f"🌍 <b>UTC:</b> {utc.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"🔗 {game['link']}",
        "",
        hashtags,
    ]

    caption = "\n".join(lines)

    # ── محدودیت ۱۰۲۴ کاراکتر تلگرام ───────────────
    if len(caption) > 1024:
        # توضیحات را کوتاه‌تر کن
        short_desc = raw_desc[:100].rstrip() + "…"
        lines[6] = short_desc
        caption  = "\n".join(lines)

        # اگر هنوز طولانی است، توضیحات را حذف کن
        if len(caption) > 1024:
            lines[5] = ""
            lines[6] = ""
            caption  = "\n".join(lines)[:1024]

    return caption


# ═══════════════════════════════════════════════════
#  TELEGRAM SENDER
# ═══════════════════════════════════════════════════
def send_game(game: dict, caption: str) -> bool:
    """ارسال عکس + کپشن به کانال تلگرام"""

    # آدرس‌های مختلف تصویر (fallback)
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

            # مشکل parse_mode → بدون HTML دوباره امتحان کن
            if "can't parse" in err.lower():
                clean_cap = _strip_html(caption)
                r2 = requests.post(api_url, data={
                    "chat_id": CHANNEL, "photo": img_url,
                    "caption": clean_cap[:1024],
                }, timeout=30)
                if r2.json().get("ok"):
                    return True

            # عکس کار نکرد → بعدی را امتحان کن
            if "wrong type" in err.lower() or "failed" in err.lower():
                continue

        except Exception as e:
            log.error(f"send_game exception: {e}")

    return False


def _strip_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text()


# ═══════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════
def main():
    log.info("═" * 55)
    log.info("  🎮 FreeGamesHub — LEGEND MODE")
    log.info("═" * 55)

    if not BOT_TOKEN or not CHANNEL:
        log.error("❌  TELEGRAM_BOT_TOKEN یا TELEGRAM_CHANNEL تنظیم نشده")
        return

    init_db()

    # ── دریافت لیست بازی‌ها ──────────────────────────
    games = fetch_games()
    if not games:
        log.error("هیچ بازی‌ای پیدا نشد!")
        return

    # مرتب‌سازی: اول ۱۰۰٪، بعد بیشترین تخفیف
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

        # ── تکراری؟ ─────────────────────────────────
        if is_sent(game_id):
            log.info("       ↪ قبلاً ارسال شده")
            skipped += 1
            continue

        # ── جزئیات از Steam API ──────────────────────
        details = get_details(game_id)
        if not details:
            log.warning(f"       ✗ جزئیات دریافت نشد")
            failed += 1
            continue

        # ── فیلتر DLC/App/Tool ──────────────────────
        app_type = details.get("type", "")
        if app_type not in ("game", ""):
            log.info(f"       ↪ نوع {app_type!r} — رد شد")
            no_valid += 1
            continue

        # ── معتبر است؟ ───────────────────────────────
        if not is_valid(game, details):
            log.info(f"       ↪ تخفیف ناکافی ({disc}%) — رد شد")
            no_valid += 1
            continue

        # ── نظرات Steam ─────────────────────────────
        rev_pct, rev_count, rev_desc = get_steam_reviews(game_id)

        # ── ساخت کپشن ───────────────────────────────
        caption = build_caption(game, details, rev_pct, rev_count, rev_desc)

        # ── ارسال به تلگرام ─────────────────────────
        ok = send_game(game, caption)

        if ok:
            mark_sent(game_id, title)
            sent_ok += 1
            log.info(f"       ✅ ارسال شد")
        else:
            failed += 1
            log.error(f"       ❌ خطا در ارسال")

        time.sleep(3)   # فاصله بین ارسال‌ها (Telegram rate limit)

    log.info("═" * 55)
    log.info(f"  ✅ ارسال شد:      {sent_ok}")
    log.info(f"  ⏭  رد شد (قبلی): {skipped}")
    log.info(f"  ⚠️  غیرمعتبر:    {no_valid}")
    log.info(f"  ❌ خطا:          {failed}")
    log.info("═" * 55)


if __name__ == "__main__":
    main()
