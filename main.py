import os
import requests
import sqlite3
import datetime
import jdatetime
from bs4 import BeautifulSoup

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

DB_FILE = "games.db"

SEARCH_URL = "https://store.steampowered.com/search/?specials=1"


# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()


def is_sent(game_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM sent WHERE id=?", (game_id,))
    row = c.fetchone()
    conn.close()
    return row is not None


def mark_sent(game_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent VALUES (?)", (game_id,))
    conn.commit()
    conn.close()


# ================= TIME =================
def now():
    utc = datetime.datetime.utcnow()
    iran = utc + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran)
    return utc, iran, shamsi


# ================= STEAM LIST =================
def fetch_games():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(SEARCH_URL, headers=headers, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")

    games = []

    for item in soup.select(".search_result_row"):
        try:
            link = item["href"]
            appid = link.split("/app/")[1].split("/")[0]

            title = item.select_one(".title").text.strip()
            img = item.select_one("img")["src"]

            discount_el = item.select_one(".discount_pct")
            price_new = item.select_one(".discount_final_price")
            price_old = item.select_one(".discount_original_price")

            discount = int(discount_el.text.replace("-", "").replace("%", "")) if discount_el else 0

            # حذف DLC و Soundtrack
            if "DLC" in title or "Soundtrack" in title:
                continue

            games.append({
                "id": appid,
                "title": title,
                "link": link,
                "image": img,
                "discount": discount,
                "price": price_new.text if price_new else "",
                "old_price": price_old.text if price_old else ""
            })

        except:
            continue

    return games


# ================= DETAILS =================
def get_details(appid):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    r = requests.get(url, timeout=10)
    data = r.json()

    if not data.get(str(appid), {}).get("success"):
        return None

    return data[str(appid)]["data"]


# ================= VALIDATION =================
def is_valid(game, details):
    if game["discount"] >= 90:
        return True

    if details and details.get("is_free"):
        return True

    return False


# ================= CAPTION =================
def build_caption(game, details):

    utc, iran, shamsi = now()

    genres = details.get("genres", [])
    genre_en = ", ".join([g["description"] for g in genres]) if genres else "Unknown"

    desc = details.get("short_description", "No description available")

    metacritic = details.get("metacritic", {}).get("score", "N/A")

    return f"""
🎮 <b>{game['title']}</b>

🎯 Genre:
{genre_en}

📝 Description:
{desc[:300]}

⭐ Steam Score:
{metacritic}

💰 Original Price:
{game['old_price']}

💸 Discount:
{game['discount']}%

📅 Iran:
{shamsi.strftime('%Y/%m/%d')} | {iran.strftime('%H:%M')}

🌍 UTC:
{utc.strftime('%Y-%m-%d %H:%M')}

🔗 {game['link']}
"""


# ================= SEND =================
def send(game, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    requests.post(url, data={
        "chat_id": CHANNEL,
        "photo": game["image"],
        "caption": caption,
        "parse_mode": "HTML"
    })


# ================= MAIN =================
def main():

    if not BOT_TOKEN or not CHANNEL:
        print("Missing ENV")
        return

    init_db()

    games = fetch_games()

    sent_count = 0

    for g in games:

        if is_sent(g["id"]):
            continue

        details = get_details(g["id"])
        if not details:
            continue

        if not is_valid(g, details):
            continue

        send(g, build_caption(g, details))

        mark_sent(g["id"])
        sent_count += 1

    print("TOTAL:", len(games))
    print("SENT:", sent_count)


if __name__ == "__main__":
    main()
