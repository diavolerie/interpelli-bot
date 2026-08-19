"""
Versione corretta del monitor interpelli.

Modifiche richieste:
- correzione del return irraggiungibile in detail_page_matches_target;
- notifiche Telegram a più chat tramite TELEGRAM_CHAT_IDS;
- nessun messaggio Telegram quando non ci sono nuovi interpelli;
- nessun listener Telegram incluso.

Questa versione conserva la logica del monitor fornito, correggendo solo
le parti sopra indicate.
"""

import json
import os
import re
import time
import hashlib
import logging
import difflib
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITES_FILE = "sites.json"
STATE_FILE = "state.json"
SCHOOLS_FILE = "schools.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [
    chat_id.strip()
    for chat_id in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if chat_id.strip()
]

REQUEST_TIMEOUT = 20
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

MAX_DETAIL_CHECKS_PER_SITE = 30
DETAIL_REQUEST_DELAY = 1.0

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("monitor_interpelli")

PROVINCIA_PATTERN = re.compile(
    r"\bprovincia\s+di\s+([a-zA-ZÀ-ÿ' ]{3,30})", re.IGNORECASE
)
COMUNE_SIGLA_PATTERN = re.compile(r"\(([A-Z]{2})\)")
COMUNE_MAIUSC_PATTERN = re.compile(r"-\s*([A-ZÀ-Ý' ]{3,40})\s*$")

SITE_TO_PROVINCIA = {
    "Bergamo": "BERGAMO",
    "Brescia": "BRESCIA",
    "Como": "COMO",
    "Cremona": "CREMONA",
    "Lecco": "LECCO",
    "Lodi": "LODI",
    "Mantova": "MANTOVA",
    "Milano": "MILANO",
    "Monza e Brianza": "MONZA E DELLA BRIANZA",
    "Pavia": "PAVIA",
    "Sondrio": "SONDRIO",
    "Varese": "VARESE",
}

CODE_PATTERN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{8}\b")
FUZZY_MATCH_THRESHOLD = 0.55
MAX_PDF_LINKS_PER_DETAIL = 3


def normalize_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def contains_any(blob, keywords):
    blob_cf = blob.casefold()
    return any(keyword.casefold() in blob_cf for keyword in keywords)


def make_id(site_name, full_url):
    raw = f"{site_name}|{full_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def find_container(a_tag):
    for name in ("tr", "li", "article"):
        found = a_tag.find_parent(name)
        if found is not None:
            return found
    return a_tag.parent


def extract_provincia_comune(text):
    provincia = None
    comune = None

    match = PROVINCIA_PATTERN.search(text)
    if match:
        provincia = normalize_text(match.group(1)).title()

    match = COMUNE_SIGLA_PATTERN.search(text)
    if match:
        comune = match.group(1)

    if not comune:
        match = COMUNE_MAIUSC_PATTERN.search(text)
        if match:
            candidate = normalize_text(match.group(1))
            if len(candidate) >= 3:
                comune = candidate.title()

    return provincia or "Non trovata", comune or "Non trovato"


def load_schools():
    if not os.path.exists(SCHOOLS_FILE):
        log.warning("%s non trovato: matching scuole disattivato.", SCHOOLS_FILE)
        return []
    with open(SCHOOLS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def build_school_indexes(schools):
    by_code = {}
    by_comune = {}

    for school in schools:
        for code in (
            school.get("codice_istituto"),
            school.get("codice_meccanografico"),
        ):
            if code:
                by_code[code.upper()] = school

        comune = school.get("comune", "").upper()
        if comune:
            by_comune.setdefault(comune, []).append(school)

    return by_code, by_comune


def site_provincia_hint(site_name):
    for suffix, provincia in SITE_TO_PROVINCIA.items():
        if site_name.endswith(suffix):
            return provincia
    return None


def find_school_by_code(text, by_code):
    for candidate in CODE_PATTERN.findall(text.upper()):
        if candidate in by_code:
            return by_code[candidate]
    return None


def find_school_by_name(title, comune_hint, site_name, by_comune):
    candidates = []

    if comune_hint and comune_hint not in ("Non trovato", "Non trovata"):
        candidates = by_comune.get(comune_hint.upper(), [])

    if not candidates:
        provincia = site_provincia_hint(site_name)
        if provincia:
            for schools_in_comune in by_comune.values():
                candidates.extend(
                    school
                    for school in schools_in_comune
                    if school.get("provincia") == provincia
                )

    if not candidates:
        return None

    title_norm = normalize_text(title)
    if comune_hint and comune_hint not in ("Non trovato", "Non trovata"):
        suffix = f"- {comune_hint}"
        if title_norm.casefold().endswith(suffix.casefold()):
            title_norm = title_norm[: -len(suffix)].strip(" -")

    title_cf = title_norm.casefold()
    best = None
    best_score = 0.0

    for school in candidates:
        for field in ("nome_scuola", "nome_istituto"):
            name = school.get(field, "")
            if not name:
                continue

            name_cf = name.casefold()
            if len(name_cf) >= 6 and (
                name_cf in title_cf or title_cf in name_cf
            ):
                score = 0.9
            else:
                score = difflib.SequenceMatcher(None, name_cf, title_cf).ratio()

            if score > best_score:
                best_score = score
                best = school

    return best if best_score >= FUZZY_MATCH_THRESHOLD else None


def match_school(title, extra_text, comune_hint, site_name, by_code, by_comune):
    school = find_school_by_code(f"{title} {extra_text}", by_code)
    if school:
        return school
    return find_school_by_name(title, comune_hint, site_name, by_comune)


def fetch(url):
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


def fetch_bytes(url):
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.content


def is_pdf_url(url):
    return url.lower().split("?")[0].endswith(".pdf")


def extract_pdf_text(pdf_bytes):
    from io import BytesIO
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return normalize_text(" ".join(parts))


def detail_page_matches_target(url, target_keywords):
    """Restituisce (match, errore, testo_segnale)."""
    try:
        if is_pdf_url(url):
            pdf_text = extract_pdf_text(fetch_bytes(url))
            return contains_any(pdf_text, target_keywords), False, pdf_text

        html = fetch(url)
    except requests.RequestException as exc:
        log.warning("Impossibile aprire %s: %s", url, exc)
        return False, True, ""
    except Exception as exc:
        log.warning("Errore leggendo %s: %s", url, exc)
        return False, True, ""

    soup = BeautifulSoup(html, "html.parser")
    body_text = normalize_text(soup.get_text(" ", strip=True))

    signal_parts = []
    title_tag = soup.find("title")
    if title_tag and title_tag.get_text(strip=True):
        signal_parts.append(normalize_text(title_tag.get_text(" ", strip=True)))

    h1_tag = soup.find("h1")
    if h1_tag and h1_tag.get_text(strip=True):
        signal_parts.append(normalize_text(h1_tag.get_text(" ", strip=True)))

    detail_signal_text = " ".join(signal_parts)

    # Il testo completo serve per riconoscere la materia/classe.
    if contains_any(body_text, target_keywords):
        return True, False, detail_signal_text

    # I PDF allegati appartengono all'annuncio corrente e sono sicuri da
    # aggiungere al testo-segnale per il matching della scuola.
    combined_body = body_text
    for anchor in soup.find_all("a", href=True):
        pdf_url = urljoin(url, anchor["href"])
        if not is_pdf_url(pdf_url):
            continue
        if len(detail_signal_text) >= MAX_PDF_LINKS_PER_DETAIL:
            break

        try:
            pdf_text = extract_pdf_text(fetch_bytes(pdf_url))
        except Exception as exc:
            log.warning("Impossibile leggere PDF allegato %s: %s", pdf_url, exc)
            continue

        combined_body = f"{combined_body} {pdf_text}"
        detail_signal_text = f"{detail_signal_text} {pdf_text}"

    # Unico return finale: evita il return morto che restituiva la pagina
    # completa al matching della scuola.
    return contains_any(combined_body, target_keywords), False, detail_signal_text


def extract_items(
    site_name,
    url,
    html,
    generic_keywords,
    target_keywords,
    known_ids,
    check_details=True,
):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_this_run = set()
    unresolved_ids = set()

    base_path = urlparse(url).path.rstrip("/")
    detail_checks_done = 0
    detail_checks_skipped = 0

    for anchor in soup.find_all("a", href=True):
        link_text = normalize_text(anchor.get_text(" ", strip=True))
        if not link_text:
            continue

        full_url = urljoin(url, anchor["href"])
        if urlparse(full_url).path.rstrip("/") == base_path:
            continue

        item_id = make_id(site_name, full_url)
        if item_id in seen_this_run:
            continue
        seen_this_run.add(item_id)

        container = find_container(anchor)
        container_text = (
            normalize_text(container.get_text(" ", strip=True))
            if container
            else ""
        )
        blob = f"{link_text} {container_text}"

        if not contains_any(blob, generic_keywords):
            continue

        target_ok = contains_any(blob, target_keywords)
        found_in_detail = False
        detail_text = ""

        if not target_ok and check_details:
            if i
