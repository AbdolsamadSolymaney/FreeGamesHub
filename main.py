import os
import requests
import json
import datetime
import jdatetime
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

STATE_FILE = "state.json"

STEAM_URL = "https://store.steampowered.com/search/?specials=1"


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
# SCRAP STEAM
# =========================
def get_games():
    headers = {"User-Agent": "Mozilla/5.0"}

    r = requests.get(STEAM_URL, headers=headers, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")

    games = []

    for item in soup.select(".search_result_row"):
        try:
            title = item.select_one(".title").text.strip()
            link = item["href"]
            game_id = link.split("/app/")[1].split("/")[0]

            discount_el = item.select_one(".discount_pct")
            price_el = item.select_one(".discount_final_price")
            old_price_el = item.select_one(".discount_original_price")
            img = item.select_one("img")["src"]

            discount = int(discount_el.text.replace("-", "").replace("%", "")) if discount_el else 0

            games.append({
                "id": game_id,
                "name": title,
                "link": link,
                "discount": discount,
                "price": price_el.text if price_el else "N/A",
                "old_price": old_price_el.text if old_price_el else "N/A",
                "image": img
            })

        except:
            continue

    return games


# =========================
# FILTER (90%+ ONLY)
# =========================
def is_valid(game):
    return game["discount"] >= 90


# =========================
# TELEGRAM SEND
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
# MESSAGE
# =========================
def build(game):
    utc, iran, shamsi = get_time()

    return f"""
🎮 <b>{game['name']}</b>

📉 Discount: {game['discount']}%

💰 Price: {game['old_price']} → {game['price']}

📅 Iran: {shamsi.strftime('%Y/%m/%d')} | {iran.strftime('%H:%M')}

🌍 UTC: {utc.strftime('%Y-%m-%d %H:%M')}

🔗 {game['link']}
"""


# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN or not CHANNEL:
        print("Missing ENV variables")
        return

    games = get_games()
    state = load_state()

    print("TOTAL GAMES:", len(games))

    sent = 0

    for g in games[:30]:  # محدود برای تست
        print(g["name"], g["discount"])

        if g["id"] in state["sent"]:
            continue

        if not is_valid(g):
            continue

        caption = build(g)
        send(g, caption)

        state["sent"].append(g["id"])
        sent += 1

    save_state(state)

    print("SENT:", sent)


if __name__ == "__main__":
    main()
