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

def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip()

def parse_row(row: str):
    """
    Esperado: 'DD/MM/YY | TICKER | DESCRIPCION ... | |'
    Devuelve: (fecha, ticker, descripcion)
    """
    parts = [p.strip() for p in row.split("|")]
    # limpiamos vacíos al final
    parts = [p for p in parts if p != ""]
    fecha = parts[0] if len(parts) > 0 else ""
    ticker = parts[1] if len(parts) > 1 else ""
    desc = parts[2] if len(parts) > 2 else ""
    return _norm(fecha), _norm(ticker), _norm(desc)

def classify_event(desc: str):
    d = desc.upper()

    # Dividendos
    if "DIVIDENDO" in d:
        return "💰 Dividendos", None

    # Cambios corporativos “accionables”
    if "DESLISTING" in d:
        return "⚙️ Cambios corporativos", "Deslisting"
    if "CAMBIO DE MERCADO" in d or ("CAMBIO" in d and "MERCADO" in d):
        return "⚙️ Cambios corporativos", "Cambio de mercado"
    if "SPLIT" in d:
        return "⚙️ Cambios corporativos", "Split"
    if "REVERSE" in d and "SPLIT" in d:
        return "⚙️ Cambios corporativos", "Reverse split"

    # Ampliaciones
    if "AMPLIACIÓN" in d or "AMPLIACION" in d:
        return "🏗 Ampliaciones", "Ampliación de monto máximo"

    # Warrant / distribuciones (si querés verlo como “corporativo”)
    if "WARRANT" in d or "DISTRIBUCIÓN" in d or "DISTRIBUCION" in d:
        return "⚙️ Cambios corporativos", "Distribución / Warrants"

    # Info relevante
    if "INFORMACIÓN RELEVANTE" in d or "INFORMACION RELEVAVANTE" in d or "INFORMACION RELEVANTE" in d:
        return "📝 Información relevante", "Información relevante"

    # Fallback
    return "📌 Otros", "Evento"

def build_message(new_items, now_str: str, url: str, max_per_cat: int = 10):
    """
    new_items: lista de filas crudas
    now_str: 'YYYY-MM-DD HH:MM' ya en hora AR
    """
    # orden de categorías
    cat_order = [
        "💰 Dividendos",
        "⚙️ Cambios corporativos",
        "🏗 Ampliaciones",
        "📝 Información relevante",
        "📌 Otros",
    ]

    buckets = {c: [] for c in cat_order}

    # Para dividendos: solo tickers únicos
    div_seen = set()
    # Para el resto: (ticker, label) únicos
    other_seen = set()

    for row in new_items:
        fecha, ticker, desc = parse_row(row)
        if not ticker:
            continue

        cat, label = classify_event(desc)

        if cat == "💰 Dividendos":
            if ticker not in div_seen:
                div_seen.add(ticker)
                buckets[cat].append(f"• {ticker}")
        else:
            lab = label or "Evento"
            key = (ticker, lab)
            if key not in other_seen:
                other_seen.add(key)
                buckets[cat].append(f"• {ticker} – {lab}")

    # construir mensaje
    lines = []
    lines.append("🔔 Nuevos eventos CEDEAR")
    lines.append("")
    lines.append(f"📅 {now_str} AR")
    lines.append("")

    any_section = False
    for cat in cat_order:
        items = buckets[cat]
        if not items:
            continue
        any_section = True
        lines.append(cat)
        lines.append("")
        lines.extend(items[:max_per_cat])
        if len(items) > max_per_cat:
            lines.append(f"• … y {len(items) - max_per_cat} más")
        lines.append("")

    if not any_section:
        return None  # nada para mandar

    lines.append(f"Fuente: {url}")
    return "\n".join(lines).strip()

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
        msg = build_message(new_items, now_str=now, url=URL, max_per_cat=10)

        if msg:
            send_telegram(msg)
            print("Enviado a Telegram.")
    else:
        print("Sin novedades.")

    save_seen(current_set)

if __name__ == "__main__":
    main()





