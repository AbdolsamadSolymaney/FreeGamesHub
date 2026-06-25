import os
import requests
import json
import datetime
import jdatetime

# ======================
# CONFIG (از GitHub Secrets)
# ======================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

STATE_FILE = "state.json"

STEAM_URL = "https://store.steampowered.com/api/featuredcategories/?cc=us&l=en"


# ======================
# STATE (جلوگیری از ارسال تکراری)
# ======================
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"sent_games": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ======================
# TIME CONVERT (UTC + IRAN + SHAMSI)
# ======================
def get_times():
    utc_now = datetime.datetime.utcnow()
    iran_time = utc_now + datetime.timedelta(hours=3, minutes=30)
    shamsi = jdatetime.datetime.fromgregorian(datetime=iran_time)
    return utc_now, iran_time, shamsi


# ======================
# TELEGRAM SEND
# ======================
def send_photo(title, image, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    data = {
        "chat_id": CHANNEL,
        "photo": image,
        "caption": caption,
        "parse_mode": "HTML"
    }

    requests.post(url, data=data)


# ======================
# STEAM FETCH
# ======================
def get_games():
    try:
        res = requests.get(STEAM_URL, timeout=10)
        data = res.json()
        return data.get("specials", {}).get("items", [])
    except:
        return []


# ======================
# FILTER (فقط 90%+ یا رایگان)
# ======================
def is_valid(game):
    discount = game.get("discount_percent", 0)
    final_price = game.get("final_price", 999999)

    if discount >= 90 or final_price == 0:
        return True

    return False


# ======================
# BUILD CAPTION (فارسی + انگلیسی + تاریخ)
# ======================
def build_caption(game):
    utc_now, iran_time, shamsi = get_times()

    title = game.get("name", "Unknown Game")
    desc = game.get("short_description", "No description available")

    price_old = game.get("original_price", 0) / 100
    price_new = game.get("final_price", 0) / 100
    discount = game.get("discount_percent", 0)

    steam_link = f"https://store.steampowered.com/app/{game.get('id')}"

    caption = f"""
🎮 <b>{title}</b>

🎯 Genre | ژانر
🇬🇧 Action / Adventure / RPG
🇮🇷 اکشن / ماجراجویی / نقش‌آفرینی

📝 Description | توضیحات

🇮🇷
{desc[:250]}

🇬🇧
{desc[:250]}

💰 Price | قیمت
{price_old}$ → {price_new}$

📉 Discount | تخفیف
{discount}%

📅 Start Time | شروع
🇮🇷 {shamsi.strftime('%Y/%m/%d')} | {iran_time.strftime('%H:%M')} (IR)
🇬🇧 {utc_now.strftime('%Y-%m-%d %H:%M')} UTC

🔗 Steam Link
{steam_link}
"""

    return caption


# ======================
# MAIN
# ======================
def main():
    if not BOT_TOKEN or not CHANNEL:
        print("Missing Secrets!")
        return

    state = load_state()
    games = get_games()

    for game in games:
        game_id = game.get("id")

        if game_id in state["sent_games"]:
            continue

        if not is_valid(game):
            continue

        caption = build_caption(game)

        send_photo(
            game.get("name"),
            game.get("header_image"),
            caption
        )

        state["sent_games"].append(game_id)

    save_state(state)


if __name__ == "__main__":
    main()
