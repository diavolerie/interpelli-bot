import json
import hashlib
import html
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SITES_PATH = BASE_DIR / "sites.json"
STATE_PATH = BASE_DIR / "state.json"

PROVINCES = [
    "bergamo", "brescia", "como", "cremona", "lecco", "lodi", "mantova",
    "milano", "monza", "monza brianza", "pavia", "sondrio", "varese"
]

PROVINCE_CODES = {
    "BG": "Bergamo",
    "BS": "Brescia",
    "CO": "Como",
    "CR": "Cremona",
    "LC": "Lecco",
    "LO": "Lodi",
    "MN": "Mantova",
    "MI": "Milano",
    "MB": "Monza Brianza",
    "PV": "Pavia",
    "SO": "Sondrio",
    "VA": "Varese"
}

def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def normalize_text(text):
    text = html.unescape(text or "")
    return re.sub(r"\s+", " ", text).strip()

def send_telegram(message):
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message[:4000],
            "disable_web_page_preview": True
        },
        timeout=30
    )
    response.raise_for_status()

def contains_any(blob, keywords):
    blob_cf = blob.casefold()
    return any(keyword.casefold() in blob_cf for keyword in keywords if keyword.strip())

def looks_relevant(text, href, generic_keywords, target_keywords):
    blob = f"{text} {href}"
    generic_ok = contains_any(blob, generic_keywords) if generic_keywords else True
    target_ok = contains_any(blob, target_keywords) if target_keywords else True
    return generic_ok and target_ok

def extract_province(text, url):
    blob = f"{text} {url}".casefold()
    host = urlparse(url).netloc.casefold()

    for prov in PROVINCES:
        if prov in blob or prov in host:
            return prov.title()

    match = re.search(r"provincia di ([A-Za-zÀ-ÖØ-öø-ÿ' -]+)", text, re.IGNORECASE)
    if match:
        return normalize_text(match.group(1)).title()

    for code, name in PROVINCE_CODES.items():
        if re.search(rf"\b{code}\b", text, re.IGNORECASE):
            return name

    return "Non trovata"

def extract_comune(text):
    text = normalize_text(text)

    patterns = [
        r"Comune di ([A-Za-zÀ-ÖØ-öø-ÿ' -]+)",
        r"\b([A-Za-zÀ-ÖØ-öø-ÿ' -]+)\s*\((BG|BS|CO|CR|LC|LO|MN|MI|MB|PV|SO|VA)\)",
        r"\b([A-Za-zÀ-ÖØ-öø-ÿ' -]+),\s*(BG|BS|CO|CR|LC|LO|MN|MI|MB|PV|SO|VA)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return normalize_text(match.group(1)).title()

    return "Non trovato"

def extract_items(site_name, base_url, html_text, generic_keywords, target_keywords):
    soup = BeautifulSoup(html_text, "html.parser")
    items = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        if not href:
            continue

        full_url = urljoin(base_url, href)

        parent_text = ""
        if a.parent:
            parent_text = normalize_text(a.parent.get_text(" ", strip=True))

        row_text = parent_text or text
        combined_text = f"{text} {row_text}"

        if not looks_relevant(combined_text, full_url, generic_keywords, target_keywords):
            continue

        provincia = extract_province(combined_text, full_url)
        comune = extract_comune(combined_text)

        raw = f"{site_name}|{text}|{full_url}"
        item_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        items.append({
            "id": item_id,
            "site": site_name,
            "title": text or "(senza titolo)",
            "url": full_url,
            "provincia": provincia,
            "comune": comune
        })

    dedup = {}
    for item in items:
        dedup[item["id"]] = item
    return list(dedup.values())

def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"
    })

    sites = load_json(SITES_PATH, [])
    state = load_json(STATE_PATH, {"seen_ids": [], "initialized": False, "last_run": None})

    old_seen = set(state.get("seen_ids", []))
    new_seen = set(old_seen)
    found_now = []
    errors = []

    for site in sites:
        if not site.get("enabled", True):
            continue

        name = site["name"]
        url = site["url"]
        generic_keywords = site.get("generic_keywords", [])
        target_keywords = site.get("target_keywords", [])

        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            items = extract_items(name, url, response.text, generic_keywords, target_keywords)

            for item in items:
                new_seen.add(item["id"])
                if item["id"] not in old_seen:
                    found_now.append(item)

        except Exception as e:
            errors.append(f"{name}: {e}")

    first_run = not state.get("initialized", False)

    if not first_run and found_now:
        found_now.sort(key=lambda x: (x["site"], x["provincia"], x["title"]))
        for item in found_now:
            msg = (
                f"Nuovo possibile interpello trovato\n"
                f"Sito: {item['site']}\n"
                f"Provincia: {item['provincia']}\n"
                f"Comune: {item['comune']}\n"
                f"Titolo: {item['title']}\n"
                f"Link: {item['url']}"
            )
            send_telegram(msg)
            time.sleep(1)

    if errors:
        send_telegram("Errori durante il controllo:\n" + "\n".join(errors[:10]))

    state["seen_ids"] = sorted(new_seen)
    state["initialized"] = True
    state["last_run"] = int(time.time())
    save_json(STATE_PATH, state)

if __name__ == "__main__":
    main()
