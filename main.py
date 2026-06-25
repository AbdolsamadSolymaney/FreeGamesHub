import os
import requests
import json
import datetime
import jdatetime
from bs4 import BeautifulSoup

# =======================
# CONFIG (GitHub Secrets)
# =======================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

STATE_FILE = "state.json"

STEAM_URL = "https://store.steampowered.com/search/?specials=1"


# =======================
# AAA LIST
# =======================
AAA_KEYWORDS = [
    "gta", "grand theft auto",
    "red dead", "rockstar",
    "cyberpunk",
    "battlefield",
    "call of duty",
    "assassin",
    "elden ring",
    "witcher",
    "horizon",
    "god of war",
    "spider-man",
    "resident evil",
    "far cry",
    "starfield",
    "hogwarts",
    "diablo",
]


def is_aaa(title):
    t = title.lower()
    return any(k in t for k in AAA_KEYWORDS)


# =======================
# SCORE SYSTEM
# =======================
def score_game(game):
    score = 0

    if game["discount"] >= 90:
        score += 50
    elif game["discount"] >= 75:
        score += 30

    if is_aaa(game["title"]):
        score += 40

    if game["price"] in ["Free", "0", "Free to Play"]:
        score += 50

    return score


def is_valid(game):
    return score_game(game) >= 60


# =======================
# STATE
# =======================
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"sent": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =======================
# TIME
# =======================
def time_info():
    utc = datetime.datetime.utcnow()
    iran = utc + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran)
    return utc, iran, shamsi


# =======================
# SCRAP STEAM
# =======================
def fetch_games():
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
            img = item.select_one("img")["src"]

            discount = int(discount_el.text.replace("-", "").replace("%", "")) if discount_el else 0

            games.append({
                "id": game_id,
                "title": title,
                "link": link,
                "discount": discount,
                "price": price_el.text if price_el else "",
                "image": img
            })

        except:
            continue

    return games


# =======================
# CAPTION
# =======================
def build_caption(game):
    utc, iran, shamsi = time_info()
    score = score_game(game)

    tag = "🔥 AAA GAME" if is_aaa(game["title"]) else "🎁 DEAL"

    return f"""
{tag} | Score: {score}/100

🎮 <b>{game['title']}</b>

💰 Discount: {game['discount']}%

📊 Score: {score}/100

📅 Iran:
{shamsi.strftime('%Y/%m/%d')} | {iran.strftime('%H:%M')}

🌍 UTC:
{utc.strftime('%Y-%m-%d %H:%M')}

🔗 {game['link']}
"""


# =======================
# TELEGRAM SEND
# =======================
def send(game, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    data = {
        "chat_id": CHANNEL,
        "photo": game["image"],
        "caption": caption,
        "parse_mode": "HTML"
    }

    requests.post(url, data=data)


# =======================
# MAIN
# =======================
def main():
    if not BOT_TOKEN or not CHANNEL:
        print("Missing ENV variables")
        return

    games = fetch_games()
    state = load_state()

    # مرتب‌سازی حرفه‌ای
    games = sorted(games, key=score_game, reverse=True)

    sent_count = 0

    for g in games:

        if g["id"] in state["sent"]:
            continue

        if not is_valid(g):
            continue

        send(g, build_caption(g))

        state["sent"].append(g["id"])
        sent_count += 1

    save_state(state)

    print("TOTAL:", len(games))
    print("SENT:", sent_count)


if __name__ == "__main__":
    main()
