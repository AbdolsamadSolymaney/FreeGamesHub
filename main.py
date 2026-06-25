import os
import requests
import json
import datetime
import jdatetime
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

STATE_FILE = "state.json"

# چند صفحه تخفیف استیم (منبع واقعی)
STEAM_DEALS_URL = "https://store.steampowered.com/search/?specials=1"


# =========================
# STATE
# =========================
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"sent": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =========================
# TIME
# =========================
def get_time():
    utc = datetime.datetime.utcnow()
    iran = utc + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran)
    return utc, iran, shamsi


# =========================
# GET STEAM DEALS (REAL SCRAPING)
# =========================
def get_deals():
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    res = requests.get(STEAM_DEALS_URL, headers=headers, timeout=10)
    soup = BeautifulSoup(res.text, "html.parser")

    games = []

    for item in soup.select(".search_result_row"):
        try:
            title = item.select_one(".title").text.strip()
            link = item["href"]
            game_id = link.split("/app/")[1].split("/")[0]

            discount = item.select_one(".discount_pct")
            price = item.select_one(".discount_final_price")
            old_price = item.select_one(".discount_original_price")
            img = item.select_one("img")["src"]

            discount_val = int(discount.text.replace("-", "").replace("%", "")) if discount else 0

            games.append({
                "id": game_id,
                "name": title,
                "link": link,
                "discount": discount_val,
                "price": price.text if price else "",
                "old_price": old_price.text if old_price else "",
                "image": img
            })
        except:
            continue

    return games


# =========================
# FILTER (ONLY 90%+)
# =========================
def is_valid(game):
    return game["discount"] >= 90


# =========================
# SEND TO TELEGRAM
# =========================
def send(game, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    data = {
        "chat_id": CHANNEL,
        "photo": game["image"],
        "caption": caption,
        "parse_mode": "HTML"
    }

    requests.post(url, data=data)


# =========================
# BUILD MESSAGE
# =========================
def build(game):
    utc, iran, shamsi = get_time()

    return f"""
🎮 <b>{game['name']}</b>

📉 Discount: {game['discount']}%

💰 Price: {game['price']}

📅 Iran: {shamsi.strftime('%Y/%m/%d')} | {iran.strftime('%H:%M')}

🌍 UTC: {utc.strftime('%Y-%m-%d %H:%M')}

🔗 {game['link']}
"""


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN or not CHANNEL:
        print("Missing env vars")
        return

    state = load_state()
    games = get_deals()

    for game in games:

        if game["id"] in state["sent"]:
            continue

        if not is_valid(game):
            continue

        caption = build(game)
        send(game, caption)

        state["sent"].append(game["id"])

    save_state(state)


if __name__ == "__main__":
    main()
