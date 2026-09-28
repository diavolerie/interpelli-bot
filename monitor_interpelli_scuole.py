"""
Bot Telegram per monitorare interpelli di supplenza per SPAGNOLO
direttamente sui siti web delle singole scuole della Lombardia
(non piu' sulle pagine MIM/USR).

Differenze principali rispetto a monitor_interpelli.py (il bot "MIM"):
- La fonte non e' piu' un elenco di 12 pagine provinciali con struttura
  omogenea, ma centinaia di siti scolastici tutti diversi tra loro
  (scuole_sites.json, generato da generate_scuole_sites.py a partire
  dal file Excel). Per questo il crawling e' "best effort": per ogni
  scuola si controlla la homepage e, se contengono nel testo del link
  una delle SECTION_KEYWORDS (circolari, albo pretorio, graduatorie,
  personale, bandi, amministrazione trasparente, ecc.), si aprono
  anche fino a MAX_SECTION_PAGES_PER_SITE pagine "figlie" trovate a
  partire dalla homepage. Non si scende oltre questo secondo livello.
- Non serve piu' abbinare l'annuncio trovato a una scuola tramite
  fuzzy-match sul nome (schools.json + difflib), perche' qui si
  itera GIA' per singola scuola nota: la scuola e' sempre certa.
- Usa un bot Telegram e uno stato (state_scuole.json) separati dal
  bot MIM, cosi' le due pipeline restano indipendenti.
- Stessa filosofia di stato "evaluated"/"notified" e primo avvio
  silenzioso del bot MIM (vedi commenti in quel file).

Rendering Javascript (novita'):
- Per ogni pagina si prova PRIMA il metodo veloce (requests + BS4).
- Se il testo visibile risultante e' sospettosamente scarso (euristica:
  meno di JS_HEURISTIC_MIN_CHARS caratteri di testo "vero", segno che
  il contenuto e' probabilmente iniettato via Javascript dopo il
  caricamento iniziale), si ritenta con un browser headless
  (Playwright/Chromium) che esegue il Javascript della pagina prima di
  leggerne l'HTML. Questo tiene il bot veloce sulla maggioranza dei
  siti (statici/WordPress) e piu' completo sui siti "app-like".
- Se Playwright non e' installato, il bot funziona comunque (fallback
  normale a solo requests), loggando un avviso.

Limiti da tenere presente (a differenza delle pagine MIM):
- Il registro elettronico (Axios, Argo, Spaggiari, ecc.) NON viene
  controllato: e' un'area privata per utenti gia' censiti, mentre gli
  interpelli per definizione devono raggiungere candidati esterni e
  quindi vengono sempre pubblicati anche sul sito pubblico della
  scuola (che e' quello che questo bot legge).
- Alcune scuole potrebbero comunque sfuggire (contenuto caricato solo
  dopo un'interazione utente, protezioni anti-bot, captcha, ecc.).
- Struttura, velocita' e affidabilita' variano molto da sito a sito:
  errori/timeout per singoli siti sono normali e vengono ritentati
  al giro successivo, non bloccano gli altri siti.
"""

import json
import os
import re
import sys
import time
import hashlib
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

SCUOLE_SITES_FILE = "scuole_sites.json"
STATE_FILE = "state_scuole.json"

# Bot/chat Telegram SEPARATI da quelli del bot MIM.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_SCUOLE", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID_SCUOLE", "")
TELEGRAM_CHAT_IDS = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]

REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# "graduatoria d'istituto" NON e' tra le parole generiche: una graduatoria
# non e' un interpello e faceva passare pagine di graduatorie.
GENERIC_KEYWORDS = ["interpello", "supplenza", "ricerca supplenti"]
TARGET_KEYWORDS = ["spagnolo", "lingua spagnola", "ac24", "ac25", "as2c", "am2c"]

# Testo dei link sulla homepage che ci suggerisce di seguire quella
# pagina come "sezione" dove potrebbero comparire gli interpelli.
SECTION_KEYWORDS = [
    "circolari", "avvisi", "albo pretorio", "albo on line", "albo on-line",
    "amministrazione trasparente", "graduatorie", "personale ata",
    "personale docente", "bandi", "supplenze", "interpelli",
    "news", "comunicazioni", "in evidenza",
]

MAX_SECTION_PAGES_PER_SITE = 4   # quante pagine "figlie" seguire dalla home
MAX_DETAIL_CHECKS_PER_SITE = 8   # quante pagine di dettaglio annuncio aprire
MAX_CONTAINER_LINKS = 3          # un blocco con piu' link di cosi' e' un elenco, non un annuncio
MAX_CONTAINER_CHARS = 700         # oltre questa lunghezza il blocco 'contenitore' del link viene ignorato
DETAIL_REQUEST_DELAY = 0.5       # secondi di pausa tra una richiesta e l'altra
SITE_REQUEST_DELAY = 0.3         # pausa tra un sito e il successivo

# --- Rendering Javascript (Playwright), fallback opportunistico ---
JS_HEURISTIC_MIN_CHARS = 250     # sotto questa soglia di testo "vero", si sospetta un sito JS
PLAYWRIGHT_NAV_TIMEOUT_MS = 15000
PLAYWRIGHT_MAX_RENDERS_PER_SITE = 6  # tetto ai render Javascript per sito (homepage+sezioni+dettagli)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("monitor_interpelli_scuole")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    PLAYWRIGHT_IMPORTABLE = False
    log.warning(
        "Playwright non installato: il bot funzionera' comunque, ma senza "
        "il fallback di rendering Javascript per i siti 'app-like'."
    )

_browser_holder = {"playwright": None, "browser": None}

def start_browser():
    """Avvia UN SOLO browser headless per l'intera esecuzione (riutilizzato
    su tutti i siti), per non pagare il costo di avvio ad ogni pagina."""
    if not PLAYWRIGHT_IMPORTABLE:
        return
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=True)
        _browser_holder["playwright"] = p
        _browser_holder["browser"] = browser
        log.info("Browser headless (Playwright/Chromium) avviato.")
    except Exception as exc:
        log.warning("Impossibile avviare Playwright/Chromium: %s. "
                     "Si procede senza rendering Javascript.", exc)
        _browser_holder["playwright"] = None
        _browser_holder["browser"] = None

def stop_browser():
    browser = _browser_holder.get("browser")
    p = _browser_holder.get("playwright")
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if p:
            p.stop()
    except Exception:
        pass

def render_with_js(url):
    """Carica url in un browser headless, aspetta il Javascript, ritorna
    l'HTML finale (o None se non disponibile/fallito)."""
    browser = _browser_holder.get("browser")
    if not browser:
        return None
    page = None
    try:
        page = browser.new_page(user_agent=REQUEST_HEADERS["User-Agent"])
        response = page.goto(url, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS, wait_until="networkidle")
        if response is not None and response.status >= 400:
            log.info("Rendering Javascript: %s risponde %d anche al browser.", url, response.status)
            return None
        html = page.content()
        return html
    except Exception as exc:
        log.info("Rendering Javascript fallito per %s: %s", url, exc)
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass

def visible_text_len(html):
    try:
        soup = BeautifulSoup(html, "html.parser")
        return len(normalize_text(soup.get_text(" ", strip=True)))
    except Exception:
        return 0

# ---------------------------------------------------------------------------
# Utility di testo (identiche al bot MIM)
# ---------------------------------------------------------------------------

def normalize_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

_kw_regex_cache = {}

def _kw_regex(kw):
    r = _kw_regex_cache.get(kw)
    if r is None:
        r = re.compile(r"(?<!\w)" + re.escape(kw.casefold()) + r"(?!\w)")
        _kw_regex_cache[kw] = r
    return r

def contains_any(blob, keywords):
    """True se il testo contiene una delle parole/frasi come parola
    intera (non come pezzo di un'altra parola): cosi' "ac24" non scatta
    dentro a "ac245" e "spagnolo" non scatta dentro a un'altra parola."""
    cf = blob.casefold()
    return any(_kw_regex(kw).search(cf) for kw in keywords)

def make_id(site_key, full_url):
    raw = f"{site_key}|{full_url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def find_container(a_tag):
    for name in ("tr", "li", "article"):
        found = a_tag.find_parent(name)
        if found is not None:
            return found
    return a_tag.parent

# ---------------------------------------------------------------------------
# Rete
# ---------------------------------------------------------------------------

def fetch(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text

NO_JS_RENDER_HOSTS = ("drive.google.com", "docs.google.com", "forms.gle", "mailupclient.com", "emailsp.com")

def _can_render(url, budget):
    if (not PLAYWRIGHT_IMPORTABLE
            or not _browser_holder.get("browser")
            or budget["js_renders_done"] >= PLAYWRIGHT_MAX_RENDERS_PER_SITE):
        return False
    host = urlparse(url).netloc.lower()
    return not any(h in host for h in NO_JS_RENDER_HOSTS)

def fetch_hybrid(url, budget):
    """Fetch 'ibrido': prova prima requests (veloce). Poi usa il browser
    headless (Playwright) in due casi:
    - il testo visibile e' sospettosamente scarso (probabile sito
      Javascript);
    - requests ha ricevuto 403 (molti siti/portali bloccano le richieste
      "da script" ma servono normalmente un browser vero). Nessun trucco
      anti-bot: se blocca anche il browser, si rinuncia.

    budget: dict condiviso per sito con contatore "js_renders_done".
    Solleva requests.RequestException se non si riesce ad ottenere la
    pagina in nessun modo.
    """
    try:
        html = fetch(url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 403 and _can_render(url, budget):
            budget["js_renders_done"] += 1
            log.info("403 su %s: riprovo con il browser.", url)
            rendered = render_with_js(url)
            if rendered and visible_text_len(rendered) >= JS_HEURISTIC_MIN_CHARS:
                return rendered
        raise

    text_len = visible_text_len(html)
    if text_len >= JS_HEURISTIC_MIN_CHARS or not _can_render(url, budget):
        return html

    budget["js_renders_done"] += 1
    log.info("Testo scarso (%d caratteri) su %s: provo rendering Javascript.", text_len, url)
    rendered_html = render_with_js(url)
    if rendered_html and visible_text_len(rendered_html) > text_len:
        return rendered_html
    return html

def fetch_bytes(url):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content

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

MAIN_SELECTORS = ["main", "article", "[role=main]", "#content", "#main",
                  ".entry-content", "#contenuto", ".content"]

def extract_main_node(soup):
    """Restituisce il nodo col contenuto principale della pagina,
    escludendo menu, piè di pagina, sidebar, script. Serve perche' i
    menu di un sito scolastico contengono spesso parole come "spagnolo"
    (indirizzi di studio) che non c'entrano col singolo annuncio."""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
        tag.decompose()
    for sel in MAIN_SELECTORS:
        node = soup.select_one(sel)
        if node is not None and len(normalize_text(node.get_text(" ", strip=True))) >= 200:
            return node
    return soup.body or soup

def detail_page_matches_target(url, target_keywords, budget):
    """Come nel bot MIM: apre la pagina di dettaglio (o il PDF) e
    controlla se contiene una target_keyword. Usa fetch_hybrid per le
    pagine HTML (fallback Javascript incluso)."""
    try:
        if is_pdf_url(url):
            pdf_bytes = fetch_bytes(url)
            text = extract_pdf_text(pdf_bytes)
            return contains_any(text, target_keywords), False
        html = fetch_hybrid(url, budget)
    except requests.RequestException as exc:
        log.warning("Impossibile aprire pagina di dettaglio %s: %s", url, exc)
        return False, True
    except Exception as exc:
        log.warning("Errore leggendo PDF %s: %s", url, exc)
        return False, True

    soup = BeautifulSoup(html, "html.parser")
    main_node = extract_main_node(soup)
    main_text = normalize_text(main_node.get_text(" ", strip=True))
    if contains_any(main_text, target_keywords):
        return True, False

    # controlla anche eventuali PDF allegati nel contenuto principale
    # (non quelli del menu/piè di pagina, uguali su tutto il sito)
    for a in main_node.find_all("a", href=True)[:10]:
        try:
            candidate_url = urljoin(url, a["href"])
        except ValueError:
            continue
        if is_pdf_url(candidate_url):
            try:
                pdf_text = extract_pdf_text(fetch_bytes(candidate_url))
            except Exception:
                continue
            if contains_any(pdf_text, target_keywords):
                return True, False

    return False, False

# ---------------------------------------------------------------------------
# Estrazione annunci da una pagina di un sito scuola
# ---------------------------------------------------------------------------

def extract_items_from_page(site_key, page_url, html, known_ids, budget):
    """Estrae candidati/rilevanti da UNA pagina HTML gia' scaricata.

    budget: dict mutabile con contatori "detail_checks_done" e
    "js_renders_done", condiviso tra le pagine dello stesso sito, per
    rispettare MAX_DETAIL_CHECKS_PER_SITE e PLAYWRIGHT_MAX_RENDERS_PER_SITE
    sull'intero sito (non per pagina).

    Ritorna (relevant_items, all_candidate_ids, unresolved_ids, section_links)
    """
    soup = BeautifulSoup(html, "html.parser")
    base_path = urlparse(page_url).path.rstrip("/")

    relevant_items = []
    all_candidate_ids = set()
    unresolved_ids = set()
    section_links = []  # (link_text, full_url) da eventualmente seguire
    seen_this_page = set()

    for a in soup.find_all("a", href=True):
        link_text = normalize_text(a.get_text(" ", strip=True))
        if not link_text:
            continue
        try:
            full_url = urljoin(page_url, a["href"])
        except ValueError:
            continue

        if urlparse(full_url).path.rstrip("/") == base_path:
            continue
        if not full_url.lower().startswith(("http://", "https://")):
            continue

        # link di "sezione" (circolari, albo pretorio, ...): candidati a
        # essere seguiti come pagina aggiuntiva da questo stesso sito
        if contains_any(link_text, SECTION_KEYWORDS):
            section_links.append((link_text, full_url))

        item_id = make_id(site_key, full_url)
        if item_id in seen_this_page:
            continue
        seen_this_page.add(item_id)

        container = find_container(a)
        container_text = normalize_text(container.get_text(" ", strip=True)) if container else ""
        # Se il blocco che contiene il link e' enorme (menu, intero elenco
        # di news, ecc.) non e' "la riga di quell'annuncio": basterebbe
        # una sola riga con "spagnolo" per far sembrare rilevanti tutti
        # i link vicini. In quel caso ci si limita al testo del link.
        # riga "vera" (tr/li/article): fino a MAX_CONTAINER_LINKS link;
        # altrimenti e' solo il genitore generico del link (div, body...)
        # e lo accettiamo solo se contiene quel link e nessun altro
        is_row = container is not None and container.name in ("tr", "li", "article")
        max_links = MAX_CONTAINER_LINKS if is_row else 1
        if container is not None and (
                len(container_text) > MAX_CONTAINER_CHARS
                or len(container.find_all("a")) > max_links):
            # troppo grande, o con troppi link: e' un elenco/menu, non
            # la riga di un singolo annuncio
            container_text = ""
        blob = f"{link_text} {container_text}"

        if not contains_any(blob, GENERIC_KEYWORDS):
            continue

        all_candidate_ids.add(item_id)

        target_ok = contains_any(blob, TARGET_KEYWORDS)
        found_in_detail = False

        if not target_ok:
            if item_id in known_ids:
                pass  # gia' valutato in run precedenti, non e' target
            elif budget["detail_checks_done"] >= MAX_DETAIL_CHECKS_PER_SITE:
                unresolved_ids.add(item_id)
            else:
                budget["detail_checks_done"] += 1
                match, error = detail_page_matches_target(full_url, TARGET_KEYWORDS, budget)
                if error:
                    unresolved_ids.add(item_id)
                elif match:
                    target_ok = True
                    found_in_detail = True
                time.sleep(DETAIL_REQUEST_DELAY)

        if not target_ok:
            continue

        relevant_items.append({
            "id": item_id,
            "title": link_text or container_text[:120],
            "url": full_url,
            "found_on_page": page_url,
            "found_in_detail": found_in_detail,
        })

    return relevant_items, all_candidate_ids, unresolved_ids, section_links

def check_site(site_key, homepage_url, known_ids):
    """Controlla homepage + fino a MAX_SECTION_PAGES_PER_SITE pagine di
    sezione collegate. Ritorna (relevant_items, all_candidate_ids,
    unresolved_ids)."""
    budget = {"detail_checks_done": 0, "js_renders_done": 0}
    all_relevant = []
    all_candidates = set()
    all_unresolved = set()

    try:
        html = fetch_hybrid(homepage_url, budget)
    except requests.RequestException as exc:
        log.warning("Impossibile aprire homepage %s: %s", homepage_url, exc)
        return None, exc  # segnala errore di sito (non di singolo annuncio)

    items, cand, unres, section_links = extract_items_from_page(
        site_key, homepage_url, html, known_ids, budget
    )
    all_relevant += items
    all_candidates |= cand
    all_unresolved |= unres

    visited_pages = {urlparse(homepage_url).path.rstrip("/")}
    followed = 0
    for link_text, url in section_links:
        if followed >= MAX_SECTION_PAGES_PER_SITE:
            break
        path_key = urlparse(url).path.rstrip("/")
        if path_key in visited_pages:
            continue
        visited_pages.add(path_key)
        followed += 1
        try:
            time.sleep(SITE_REQUEST_DELAY)
            sub_html = fetch_hybrid(url, budget)
        except requests.RequestException as exc:
            log.info("Sezione non raggiungibile (%s) su %s: %s", link_text, url, exc)
            continue
        items, cand, unres, _ = extract_items_from_page(
            site_key, url, sub_html, known_ids, budget
        )
        all_relevant += items
        all_candidates |= cand
        all_unresolved |= unres

    # lo stesso link puo' comparire su piu' pagine (home + sezioni):
    # va notificato una volta sola
    unique = {}
    for it in all_relevant:
        unique.setdefault(it["id"], it)
    all_relevant = list(unique.values())

    return (all_relevant, all_candidates, all_unresolved), None

# ---------------------------------------------------------------------------
# Stato persistente (stessa logica del bot MIM)
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_SEND_DELAY = 1.2      # pausa dopo ogni messaggio (Telegram limita a ~1 msg/sec per chat)
TELEGRAM_MAX_ATTEMPTS = 4      # tentativi per messaggio se Telegram risponde 429

def send_telegram_message(text):
    """Invia il messaggio a tutte le chat configurate, rispettando i
    limiti di Telegram: pausa tra un invio e l'altro e, se arriva un
    429 (Too Many Requests), attende il tempo indicato da Telegram
    (parameters.retry_after) e riprova. Se un messaggio non riesce
    proprio, il chiamante non lo segna come notificato e verra' rimandato
    al giro successivo."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        log.warning("Telegram (scuole) non configurato (token/chat_id mancanti), skip invio.")
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    at_least_one_ok = False
    for chat_id in TELEGRAM_CHAT_IDS:
        for attempt in range(1, TELEGRAM_MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    api_url,
                    data={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 429:
                    try:
                        wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        wait = 5
                    log.warning("Telegram 429 per chat %s: attendo %ds (tentativo %d/%d).",
                                chat_id, wait, attempt, TELEGRAM_MAX_ATTEMPTS)
                    time.sleep(wait + 1)
                    continue
                resp.raise_for_status()
                at_least_one_ok = True
                break
            except requests.RequestException as exc:
                log.error("Errore invio Telegram (scuole) a chat %s: %s", chat_id, exc)
                break
        time.sleep(TELEGRAM_SEND_DELAY)
    return at_least_one_ok

def notify_new_item(site_entry, item):
    detail_note = " (trovato nel dettaglio)" if item["found_in_detail"] else ""
    scuola = site_entry["schools"][0] if site_entry.get("schools") else {}
    nome = scuola.get("nome_scuola") or scuola.get("nome_istituto") or site_entry["site_name"]
    comune = scuola.get("comune") or ""
    provincia = scuola.get("provincia") or ""

    lines = [
        f"📢 Nuovo interpello spagnolo{detail_note} (sito scuola)",
        f"🏫 {nome}",
    ]
    if comune or provincia:
        lines.append(f"📍 {comune} ({provincia})")
    lines.append(f"Titolo: {item['title']}")
    if site_entry.get("url"):
        lines.append(f"🌐 {site_entry['url']}")
    if scuola.get("telefono"):
        lines.append(f"📞 {scuola['telefono']}")
    if scuola.get("link_maps"):
        lines.append(f"🗺️ {scuola['link_maps']}")
    lines.append(f"🔗 {item['url']}")
    text = "\n".join(lines)
    return send_telegram_message(text)

def notify_error(site_name, exc):
    text = f"⚠️ [scuole] Errore controllando {site_name}: {exc}"
    log.error(text)
    # Gli errori sui singoli siti scuola sono frequenti e attesi (siti
    # lenti/irraggiungibili): NON li inviamo su Telegram per non fare
    # spam, li logghiamo soltanto. Decommenta la riga sotto se invece
    # li vuoi comunque notificare.
    # send_telegram_message(text)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_scuole_sites():
    with open(SCUOLE_SITES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def main():
    sites = load_scuole_sites()
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"evaluated": {}, "notified": {}}
        log.info(
            "Primo avvio rilevato: gli interpelli spagnolo gia' attivi trovati "
            "ora verranno notificati (comportamento diverso dal bot MIM)."
        )

    log.info("Siti scuola da controllare: %d", len([s for s in sites if s.get("enabled", True)]))

    start_browser()
    try:
        _run_all_sites(sites, state, first_run)
    finally:
        stop_browser()

    save_state(state)
    log.info("Esecuzione completata.")

def _run_all_sites(sites, state, first_run):
    """first_run e' passato per coerenza con la struttura del bot MIM e
    per loggare/riconoscere il primo giro, ma qui NON cambia il
    comportamento di notifica: a differenza del bot MIM (che parte gia'
    con uno storico), questo bot-scuole parte da zero, quindi anche gli
    interpelli spagnolo attivi trovati al primo avvio vengono notificati
    subito (scelta esplicita dell'utente: potrebbero essere opportunita'
    reali gia' aperte, non ha senso scartarle in silenzio)."""
    for site in sites:
        if not site.get("enabled", True):
            continue

        site_key = site["url"]  # usato come namespace per lo stato/id
        site_name = site["site_name"]

        evaluated_ids = set(state["evaluated"].get(site_key, []))
        notified_ids = set(state["notified"].get(site_key, []))

        result, error = check_site(site_key, site["url"], evaluated_ids)
        if error is not None:
            notify_error(site_name, error)
            time.sleep(SITE_REQUEST_DELAY)
            continue

        relevant_items, all_candidate_ids, unresolved_ids = result
        new_relevant_items = [it for it in relevant_items if it["id"] not in notified_ids]

        actually_notified_ids = set()
        for item in new_relevant_items:
            if notify_new_item(site, item):
                actually_notified_ids.add(item["id"])
            # Se l'invio fallisce l'id NON entra in actually_notified_ids:
            # al prossimo run sara' di nuovo tra i "nuovi da notificare".

        evaluated_ids |= (all_candidate_ids - unresolved_ids)
        notified_ids |= actually_notified_ids

        state["evaluated"][site_key] = sorted(evaluated_ids)
        state["notified"][site_key] = sorted(notified_ids)

        if new_relevant_items:
            log.info("%s: %d nuovi annunci rilevanti.", site_name, len(new_relevant_items))

        time.sleep(SITE_REQUEST_DELAY)

if __name__ == "__main__":
    main()
