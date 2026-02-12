import json
import os
import urllib.request
import urllib.parse
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime
from zoneinfo import ZoneInfo

SOURCES = {
    "📌 Últimos eventos corporativos": "https://www.comafi.com.ar/custodiaglobal/eventos-corporativos.aspx",
    "💰 Últimos avisos de dividendos": "https://www.comafi.com.ar/custodiaglobal/dividendos.aspx",
    "🏦 Últimos pagos": "https://www.comafi.com.ar/custodiaglobal/pagos.aspx",
}

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
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        # compat: si antes era lista, lo convertimos a dict "legacy"
        if isinstance(data, list):
            return {"legacy": data}
        return data

def save_seen(seen_dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_dict, f, ensure_ascii=False, indent=2)

def scrape_rows(url: str, max_load_more_clicks=5):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

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
    
def build_multi_source_message(results_by_section, now_str):
    """
    results_by_section: dict {section_title: {"items": [str], "url": str}}
    """
    # si no hay nada nuevo en ninguna, no mandamos
    if not any(v["items"] for v in results_by_section.values()):
        return None

    lines = []
    lines.append("🔔 Novedades CEDEAR")
    lines.append("")
    lines.append(f"📅 {now_str} AR")
    lines.append("")

    for section_title, payload in results_by_section.items():
        items = payload["items"]
        url = payload["url"]
        if not items:
            continue

        lines.append(section_title)
        lines.append("")
        # ya vienen como bullets
        lines.extend(items[:20])
        if len(items) > 20:
            lines.append(f"• … y {len(items) - 20} más")
        lines.append("")
        lines.append(f"Fuente: {url}")
        lines.append("")
    # 👇 FIRMA ACÁ
    lines.append("—")
    lines.append("Equipo RIG Valores")
    lines.append("")
    lines.append("Este es un mensaje automático generado por nuestro sistema de monitoreo.")
    lines.append("Ante cualquier inquietud, contacte con su asesor.")
    lines.append("")
        
    return "\n".join(lines).strip()

def main():
    seen_dict = load_seen()  # dict por sección
    results = {}

    for section_title, url in SOURCES.items():
        current_rows = scrape_rows(url)
        if not current_rows:
            print(f"No se pudieron leer eventos en: {url}")
            results[section_title] = {"items": [], "url": url}
            continue

        seen_list = seen_dict.get(url, [])
        seen_set = set(seen_list)

        new_rows = [r for r in current_rows if r not in seen_set]

        # Convertimos a bullets cortos: "FECHA | TICKER | DESC"
        bullets = []
        for row in new_rows:
            fecha, ticker, desc = parse_row(row)  # tu parse_row actual
            if ticker:
                # texto corto
                short = desc
                # opcional: recortar largo
                if len(short) > 90:
                    short = short[:87] + "..."
                bullets.append(f"• {fecha} | {ticker} | {short}")
            else:
                bullets.append(f"• {row}")

        results[section_title] = {"items": bullets, "url": url}

        # actualizar estado de ESA url
        seen_dict[url] = current_rows

    now = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).strftime("%Y-%m-%d %H:%M")
    msg = build_multi_source_message(results, now_str=now)
    if msg:
        send_telegram(msg)
        print("Enviado a Telegram.")
    else:
        print("Sin novedades.")

    save_seen(seen_dict)

if __name__ == "__main__":
    main()







