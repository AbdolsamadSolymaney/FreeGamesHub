import os
import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL")

def send_test():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    data = {
        "chat_id": CHANNEL,
        "text": "🔥 TEST MESSAGE: Bot is working correctly!",
    }

    r = requests.post(url, data=data)
    print(r.text)

if __name__ == "__main__":
    send_test()
