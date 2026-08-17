import json
import hashlib
import html
import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SITES_PATH = BASE_DIR / "sites.json"
STATE_PATH = BASE_DIR / "state.json"

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

def looks_relevant(text, href, keywords):
    blob = f"{text} {href}".lower()
    return any(keyword.lower() in blob for keyword in keywords)

def extract_items(site_name, base_url, html_text, keywords):
    soup = BeautifulSoup(html_text, "html.parser")
    items = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        if not href:
            continue

        full_url = urljoin(base_url, href)

        if not looks_relevant(text, full_url, keywords):
            continue

        raw = f"{site_name}|{text}|{full_url}"
        item_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        items.append({
            "id": item_id,
            "site": site_name,
            "title": text or "(senza titolo)",
            "url": full_url
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
        keywords = site.get("keywords", [])

        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            items = extract_items(name, url, r.text, keywords)

            for item in items:
                new_seen.add(item["id"])
                if item["id"] not in old_seen:
                    found_now.append(item)

        except Exception as e:
            errors.append(f"{name}: {e}")

    first_run = not state.get("initialized", False)

    if not first_run and found_now:
        found_now.sort(key=lambda x: (x["site"], x["title"]))
        for item in found_now:
            msg = (
                f"Nuovo possibile interpello trovato\n"
                f"Sito: {item['site']}\n"
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
