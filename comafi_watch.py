import json
import os
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime
from zoneinfo import ZoneInfo

URL = "https://www.comafi.com.ar/custodiaglobal/eventos-corporativos.aspx"
STATE_FILE = "seen.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(
        url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()

def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))

def save_seen(items):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(items), f, ensure_ascii=False, indent=2)

def scrape_rows(max_load_more_clicks=5):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle")

        for _ in range(max_load_more_clicks):
            btn = page.get_by_text("Ver más", exact=True)
            if btn.count() == 0:
                break
            try:
                btn.first.click(timeout=1500)
                page.wait_for_timeout(1000)
            except:
                break

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    rows = []
    for tr in table.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        cells = [td.get_text(strip=True) for td in tds]
        if len(cells) < 3:
            continue
        header_text = " ".join(cells).lower()
        if "fecha" in header_text and "identificación" in header_text:
            continue
        rows.append(" | ".join(cells))

    return list(dict.fromkeys(rows))

def main():
    current_rows = scrape_rows()
    if not current_rows:
        print("No se pudieron leer eventos.")
        return

    seen = load_seen()
    current_set = set(current_rows)
    new_items = [r for r in current_rows if r not in seen]

    if new_items:
        now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%Y-%m-%d %H:%M")
        message = f"📌 Nuevos eventos CEDEAR ({now})\n\n"
        message += "\n".join(f"• {item}" for item in new_items[:20])
        message += f"\n\nFuente: {URL}"
        send_telegram(message)
        print("Enviado a Telegram.")
    else:
        print("Sin novedades.")

    save_seen(current_set)

if __name__ == "__main__":
    main()



