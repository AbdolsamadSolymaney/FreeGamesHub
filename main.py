import requests
import json
import datetime
import jdatetime

# === CONFIG ===
BOT_TOKEN = "YOUR_TOKEN"
CHANNEL = "@FreeGamesHubAlert"

STEAM_URL = "https://store.steampowered.com/api/featuredcategories/?cc=us&l=en"

STATE_FILE = "state.json"


# === LOAD STATE ===
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"sent": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# === DATE CONVERTER ===
def convert_time():
    now = datetime.datetime.utcnow()
    iran_time = now + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran_time)

    return iran_time, shamsi


# === SEND TO TELEGRAM ===
def send_to_telegram(title, image, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL,
        "photo": image,
        "caption": caption,
        "parse_mode": "HTML"
    }
    requests.post(url, data=payload)


# === GET STEAM DATA ===
def get_games():
    data = requests.get(STEAM_URL).json()

    specials = data.get("specials", {}).get("items", [])
    free = data.get("specials", {}).get("items", [])

    return specials


# === FILTER GAME ===
def is_valid(game):
    discount = game.get("discount_percent", 0)
    price = game.get("final_price", 999)

    # فقط 90%+ یا رایگان
    if discount >= 90 or price == 0:
        return True

    return False


# === BUILD CAPTION ===
def build_caption(game):
    iran_time, shamsi = convert_time()

    title = game.get("name")
    desc = game.get("short_description", "No description available")

    caption = f"""
🎮 <b>{title}</b>

🇮🇷 توضیحات:
{desc[:250]}

🇬🇧 Description:
{desc[:250]}

💰 Price: {game.get('original_price', 0)/100}$ → {game.get('final_price', 0)/100}$

📉 Discount: {game.get('discount_percent', 0)}%

📅 Iran Time:
{shamsi.strftime('%Y/%m/%d')} | {iran_time.strftime('%H:%M')}

🌍 UTC:
{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M')}

🔗 https://store.steampowered.com/app/{game.get('id')}
"""
    return caption


# === MAIN ===
def main():
    state = load_state()
    games = get_games()

    for game in games:
        game_id = game.get("id")

        if game_id in state["sent"]:
            continue

        if not is_valid(game):
            continue

        caption = build_caption(game)

        send_to_telegram(
            game.get("name"),
            game.get("header_image"),
            caption
        )

        state["sent"].append(game_id)

    save_state(state)


if __name__ == "__main__":
    main()
