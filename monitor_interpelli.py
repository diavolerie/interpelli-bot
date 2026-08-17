"""
Bot Telegram per monitorare interpelli di supplenza per SPAGNOLO
sulle pagine provinciali MIM/USR Lombardia.

Logica principale:
- sites.json definisce le pagine da controllare, ciascuna con
  generic_keywords (es. "interpello", "supplenza") e
  target_keywords (es. "spagnolo", "ac24", "ac25", "as2c", "am2c").
- Un annuncio e' "candidato" se matcha almeno una generic_keyword
  nel testo dell'elenco (AND non richiesto qui, solo generic).
- Un candidato e' "rilevante" (spagnolo) se matcha almeno una
  target_keyword nel testo dell'elenco, OPPURE se il match si trova
  solo aprendo la pagina di dettaglio del singolo annuncio (perche'
  alcune province, es. Milano, non mettono la classe di concorso
  nel titolo dell'elenco).
- state.json tiene traccia di:
    "evaluated": { site_name: [id, id, ...] } -> candidati gia'
        controllati almeno una volta (rilevanti o no), per non
        riaprire la pagina di dettaglio ad ogni esecuzione.
    "notified": { site_name: [id, id, ...] } -> annunci rilevanti
        per cui e' gia' stata inviata la notifica Telegram.
  Questa separazione evita sia di ricontrollare in dettaglio
  candidati gia' scartati, sia di notificare due volte lo stesso
  annuncio.
- Al primo avvio (state.json assente) non vengono inviate notifiche:
  si popola solo lo stato, per evitare spam con tutto lo storico.
- Ogni <a href> viene deduplicato per item_id (site_name+full_url)
  PRIMA di qualsiasi controllo, per evitare di contare/controllare
  più volte lo stesso annuncio se il link compare ripetuto nella
  pagina (menu, sidebar, paginazione, ecc.).
"""

import json
import os
import re
import sys
import time
import hashlib
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

SITES_FILE = "sites.json"
STATE_FILE = "state.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

REQUEST_TIMEOUT = 20
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Tetto di sicurezza sul numero di pagine di dettaglio aperte per sito
# ad ogni esecuzione (per restare "educati" col sito e veloci su CI).
MAX_DETAIL_CHECKS_PER_SITE = 30
DETAIL_REQUEST_DELAY = 1.0  # secondi di pausa tra un dettaglio e l'altro

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor_interpelli")

# Pattern per provare a estrarre provincia/comune dal testo dell'annuncio
PROVINCIA_PATTERN = re.compile(
    r"\bprovincia\s+di\s+([a-zA-ZÀ-ÿ' ]{3,30})", re.IGNORECASE
)
COMUNE_SIGLA_PATTERN = re.compile(r"\(([A-Z]{2})\)")
COMUNE_MAIUSC_PATTERN = re.compile(r"-\s*([A-ZÀ-Ý' ]{3,40})\s*$")

# ---------------------------------------------------------------------------
# Utility di testo
# ---------------------------------------------------------------------------

def normalize_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

def contains_any(blob, keywords):
    """True se almeno una keyword compare in blob (case-insensitive)."""
    cf = blob.casefold()
    return any(kw.casefold() in cf for kw in keywords)

def make_id(site_name, full_url):
    """ID stabile per un annuncio: dipende solo da sito+URL, non dal
    testo del link (che puo' cambiare leggermente senza che l'annuncio
    sia davvero nuovo)."""
    raw = f"{site_name}|{full_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def find_container(a_tag):
    """Risale nell'albero HTML per trovare il contenitore piu' preciso
    possibile per l'annuncio: prima una riga di tabella, poi un
    elemento di lista, poi un article, e solo come ultima spiaggia il
    parent diretto del link."""
    for name in ("tr", "li", "article"):
        found = a_tag.find_parent(name)
        if found is not None:
            return found
    return a_tag.parent

def extract_provincia_comune(text):
    provincia = None
    comune = None

    m = PROVINCIA_PATTERN.search(text)
    if m:
        provincia = normalize_text(m.group(1)).title()

    m = COMUNE_SIGLA_PATTERN.search(text)
    if m:
        comune = m.group(1)

    if not comune:
        m = COMUNE_MAIUSC_PATTERN.search(text)
        if m:
            candidate = normalize_text(m.group(1))
            # Evita di prendere sigle corte o parole comuni come "SPAGNOLO"
            if len(candidate) >= 3:
                comune = candidate.title()

    return (provincia or "Non trovata"), (comune or "Non trovato")

# ---------------------------------------------------------------------------
# Rete
# ---------------------------------------------------------------------------

def fetch(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text

def fetch_bytes(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content

def is_pdf_url(url):
    return url.lower().split("?")[0].endswith(".pdf")

def extract_pdf_text(pdf_bytes):
    """Estrae il testo da un PDF (nessun OCR: funziona solo se il PDF ha
    testo selezionabile, non se e' una scansione/immagine)."""
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

MAX_PDF_LINKS_PER_DETAIL = 3  # quanti PDF allegati controllare per ogni pagina di dettaglio

def detail_page_matches_target(url, target_keywords):
    """Apre la pagina di dettaglio (o il PDF, se il link porta direttamente
    a un PDF) e controlla se contiene una target_keyword. Se la pagina di
    dettaglio e' HTML, controlla anche fino a MAX_PDF_LINKS_PER_DETAIL PDF
    eventualmente allegati.

    Ritorna una tupla (match, errore). errore=True solo se il fetch della
    RISORSA PRINCIPALE fallisce (link diretto al PDF, o pagina HTML): in
    tal caso l'annuncio va ritentato al prossimo run. Un fallimento nel
    leggere un singolo PDF allegato secondario viene solo loggato e non
    blocca il resto (l'HTML e' comunque stato controllato).
    """
    try:
        if is_pdf_url(url):
            pdf_bytes = fetch_bytes(url)
            text = extract_pdf_text(pdf_bytes)
            return contains_any(text, target_keywords), False

        html = fetch(url)
    except requests.RequestException as exc:
        log.warning("Impossibile aprire pagina di dettaglio %s: %s", url, exc)
        return False, True
    except Exception as exc:
        log.warning("Errore leggendo PDF %s: %s", url, exc)
        return False, True

    soup = BeautifulSoup(html, "html.parser")
    text = normalize_text(soup.get_text(" ", strip=True))
    if contains_any(text, target_keywords):
        return True, False

    pdf_links = [
        urljoin(url, a["href"])
        for a in soup.find_all("a", href=True)
        if is_pdf_url(urljoin(url, a["href"]))
    ]
    for pdf_url in pdf_links[:MAX_PDF_LINKS_PER_DETAIL]:
        try:
            pdf_bytes = fetch_bytes(pdf_url)
            pdf_text = extract_pdf_text(pdf_bytes)
        except Exception as exc:
            log.warning("Impossibile leggere PDF allegato %s: %s", pdf_url, exc)
            continue
        if contains_any(pdf_text, target_keywords):
            return True, False

    return False, False

# ---------------------------------------------------------------------------
# Estrazione annunci da una pagina-elenco
# ---------------------------------------------------------------------------

def extract_items(
    site_name,
    url,
    html,
    generic_keywords,
    target_keywords,
    known_ids,
    check_details=True,
):
    """Estrae gli annunci rilevanti (spagnolo) da una pagina-elenco.

    known_ids: set di id gia' VALUTATI in precedenza (rilevanti o no) -
        per questi non si riapre mai la pagina di dettaglio, anche se
        non sono ancora stati notificati.
    check_details: se True, per i candidati nuovi che non matchano il
        target nel testo dell'elenco, prova ad aprire la pagina di
        dettaglio (fino al tetto MAX_DETAIL_CHECKS_PER_SITE).

    Ogni <a href> viene deduplicato per item_id PRIMA di ogni controllo,
    cosi' un link ripetuto piu' volte nella pagina (menu, sidebar,
    paginazione) viene elaborato una sola volta per esecuzione.

    Ritorna una lista di dict:
    {id, title, province, comune, url, found_in_detail}
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_this_run = set()
    unresolved_ids = set()  # candidati NON verificati (tetto raggiunto o errore rete):
                             # non vanno marcati "evaluated", cosi' si ritentano al prossimo run

    detail_checks_done = 0
    detail_checks_skipped = 0

    for a in soup.find_all("a", href=True):
        link_text = normalize_text(a.get_text(" ", strip=True))
        if not link_text:
            continue

        full_url = urljoin(url, a["href"])
        item_id = make_id(site_name, full_url)

        if item_id in seen_this_run:
            continue
        seen_this_run.add(item_id)

        container = find_container(a)
        container_text = normalize_text(
            container.get_text(" ", strip=True)
        ) if container else ""

        blob = f"{link_text} {container_text}"

        if not contains_any(blob, generic_keywords):
            continue  # non e' nemmeno un annuncio generico rilevante

        found_in_detail = False
        target_ok = contains_any(blob, target_keywords)

        if not target_ok and check_details:
            already_evaluated = item_id in known_ids
            if already_evaluated:
                # Gia' controllato in una run precedente: non e' target,
                # non serve riaprire la pagina di dettaglio.
                pass
            elif detail_checks_done >= MAX_DETAIL_CHECKS_PER_SITE:
                detail_checks_skipped += 1
                unresolved_ids.add(item_id)
            else:
                detail_checks_done += 1
                detail_match, detail_error = detail_page_matches_target(full_url, target_keywords)
                if detail_error:
                    unresolved_ids.add(item_id)
                elif detail_match:
                    target_ok = True
                    found_in_detail = True
                time.sleep(DETAIL_REQUEST_DELAY)

        if not target_ok:
            continue

        provincia, comune = extract_provincia_comune(container_text or blob)

        results.append({
            "id": item_id,
            "title": link_text or container_text[:120],
            "province": provincia,
            "comune": comune,
            "url": full_url,
            "found_in_detail": found_in_detail,
        })

    if check_details:
        log.info(
            "%s: controlli dettaglio eseguiti=%d, saltati per tetto massimo "
            "(MAX_DETAIL_CHECKS_PER_SITE=%d)=%d",
            site_name, detail_checks_done, MAX_DETAIL_CHECKS_PER_SITE, detail_checks_skipped,
        )

    return results, unresolved_ids

def collect_evaluated_candidate_ids(site_name, url, html, generic_keywords):
    """Ritorna gli id di TUTTI i candidati generici presenti nella pagina
    (matchano generic_keywords), a prescindere dal target. Usato per
    aggiornare lo stato 'evaluated' cosi' i prossimi run non riaprono
    inutilmente le pagine di dettaglio di candidati gia' visti e
    scartati."""
    soup = BeautifulSoup(html, "html.parser")
    ids = set()

    for a in soup.find_all("a", href=True):
        link_text = normalize_text(a.get_text(" ", strip=True))
        if not link_text:
            continue
        full_url = urljoin(url, a["href"])
        container = find_container(a)
        container_text = normalize_text(
            container.get_text(" ", strip=True)
        ) if container else ""
        blob = f"{link_text} {container_text}"
        if contains_any(blob, generic_keywords):
            ids.add(make_id(site_name, full_url))

    return ids

# ---------------------------------------------------------------------------
# Stato persistente
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return None  # None = primo avvio, distinto da stato vuoto {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Retro-compatibilita' con vecchio formato state.json (lista piatta
    # di id rilevanti, senza distinzione evaluated/notified).
    if "evaluated" not in data or "notified" not in data:
        old_ids = data if isinstance(data, dict) else {}
        cleaned = {
            k: list(v)
            for k, v in old_ids.items()
            if isinstance(v, (list, tuple, set))
        }
        data = {
            "evaluated": cleaned,
            "notified": cleaned.copy(),
        }

    return data

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram non configurato (token/chat_id mancanti), skip invio.")
        return False
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Errore invio Telegram: %s", exc)
        return False

def notify_new_item(site_name, item):
    detail_note = " (classe trovata nel dettaglio)" if item["found_in_detail"] else ""
    text = (
        f"📢 Nuovo interpello spagnolo{detail_note}\n"
        f"Sito: {site_name}\n"
        f"Titolo: {item['title']}\n"
        f"Provincia: {item['province']} | Comune: {item['comune']}\n"
        f"{item['url']}"
    )
    return send_telegram_message(text)

def notify_error(site_name, exc):
    text = f"⚠️ Errore controllando {site_name}: {exc}"
    log.error(text)
    send_telegram_message(text)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_sites():
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    sites = load_sites()
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"evaluated": {}, "notified": {}}
        log.info("Primo avvio rilevato: nessuna notifica verra' inviata in questa run.")

    for site in sites:
        if not site.get("enabled", True):
            continue

        site_name = site["name"]
        url = site["url"]
        generic_keywords = site.get("generic_keywords", [])
        target_keywords = site.get("target_keywords", [])

        evaluated_ids = set(state["evaluated"].get(site_name, []))
        notified_ids = set(state["notified"].get(site_name, []))

        log.info("Controllo sito: %s (%s)", site_name, url)

        try:
            html = fetch(url)
        except requests.RequestException as exc:
            notify_error(site_name, exc)
            continue

        # 1) aggiorna 'evaluated' con TUTTI i candidati generici visti in
        # questa pagina (cosi' i futuri run non li ri-processano in
        # dettaglio se non sono target).
        all_candidate_ids = collect_evaluated_candidate_ids(
            site_name, url, html, generic_keywords
        )

        # 2) estrai gli annunci rilevanti (spagnolo), aprendo il dettaglio
        # solo per i candidati NON ancora in evaluated_ids.
        relevant_items, unresolved_ids = extract_items(
            site_name=site_name,
            url=url,
            html=html,
            generic_keywords=generic_keywords,
            target_keywords=target_keywords,
            known_ids=evaluated_ids,
            check_details=True,
        )

        new_relevant_items = [it for it in relevant_items if it["id"] not in notified_ids]

        actually_notified_ids = set()
        if not first_run:
            for item in new_relevant_items:
                if notify_new_item(site_name, item):
                    actually_notified_ids.add(item["id"])
                # Se l'invio fallisce (token mancante, Telegram giu', ecc.)
                # l'id NON entra in actually_notified_ids: al prossimo run
                # sara' di nuovo tra i "nuovi da notificare" e si ritentera'.
        else:
            log.info(
                "%s: %d annunci rilevanti trovati al primo avvio (non notificati).",
                site_name, len(new_relevant_items),
            )
            # Al primo avvio non notifichiamo di proposito: questi id vanno
            # comunque segnati come "notified" per non spammare tutto lo
            # storico appena il bot va a regime.
            actually_notified_ids = {it["id"] for it in new_relevant_items}

        # aggiorna stato: evaluated = unione di tutto cio' che e' stato
        # VERAMENTE verificato (target trovato nell'elenco, o dettaglio
        # aperto con successo) - esclude gli id saltati per tetto massimo
        # o per errore di rete, che vanno ritentati al prossimo run;
        # notified = id per cui l'invio Telegram e' davvero riuscito (o
        # gli id gia' notificati/presenti al primo avvio).
        evaluated_ids |= (all_candidate_ids - unresolved_ids)
        notified_ids |= actually_notified_ids

        state["evaluated"][site_name] = sorted(evaluated_ids)
        state["notified"][site_name] = sorted(notified_ids)

    save_state(state)
    log.info("Esecuzione completata.")

if __name__ == "__main__":
    main()
