"""
Genera scuole_sites.json a partire dal file Excel
"SCUOLE_SECONDARIE_I_E_II_GRADO_LOMBARDIA.xlsx".

Uso:
    python generate_scuole_sites.py percorso/al/file.xlsx [--all]

Di default include SOLO le scuole marcate "SPA" nella colonna
"SPA/NO SPA" (cioe' con indirizzo/cattedra di spagnolo), perche' sono
quelle su cui ha senso aspettarsi un interpello per la classe di
concorso di spagnolo. Con --all include tutte le scuole del file
(molto piu' lento da controllare ad ogni esecuzione del bot).

Il file Excel ha una riga per "sede" (una scuola/istituto puo' avere
piu' sedi/plessi); istituti diversi possono anche condividere lo
stesso sito web (raro) e la stessa sede puo' avere piu' righe (una per
grado/indirizzo). Per evitare di controllare piu' volte lo stesso URL,
l'output e' raggruppato per SITO WEB normalizzato: ogni voce contiene
l'elenco delle scuole/sedi collegate a quel sito.

Le righe senza un sito web valido vengono scartate (segnalate a fine
esecuzione) perche' non c'e' nulla da controllare per loro.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from urllib.parse import urlparse

import openpyxl

COLONNE_ATTESE = [
    "NOME ISTITUTO", "NOME SCUOLA", "TIPO ISTRUZIONE",
    "TIPO ISTITUTO DETTAGLIATO", "SPA/NO SPA", "SITO WEB SCUOLA",
    "EMAIL", "CODICE ISTITUTO", "CODICE MECCANOGRAFICO SCUOLA",
    "PROVINCIA", "COMUNE", "INDIRIZZO", "TELEFONO", "LINK GOOGLE MAPS",
]

def normalize_url_key(url):
    """Chiave di deduplica: stesso dominio+path a prescindere da
    http/https, www, maiuscole o slash finale."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    if not netloc:
        return None
    return f"{netloc}{path}"

def pick_display_url(urls):
    """Tra le varianti di URL raccolte per la stessa chiave normalizzata,
    preferisce https, poi quella con 'www.', poi la prima incontrata."""
    def score(u):
        s = 0
        if u.lower().startswith("https://"):
            s += 2
        if "://www." in u.lower():
            s += 1
        return s
    return sorted(urls, key=score, reverse=True)[0]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("excel_path")
    parser.add_argument(
        "--all", action="store_true",
        help="Includi tutte le scuole (default: solo quelle SPA).",
    )
    parser.add_argument(
        "-o", "--output", default="scuole_sites.json",
        help="File di output (default: scuole_sites.json)",
    )
    args = parser.parse_args()

    wb = openpyxl.load_workbook(args.excel_path, read_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = next(rows)

    idx = {h: i for i, h in enumerate(header) if h}
    mancanti = [c for c in COLONNE_ATTESE if c not in idx]
    if mancanti:
        print(f"ATTENZIONE: colonne attese non trovate nel file: {mancanti}",
              file=sys.stderr)

    # site_key -> { "urls": set(), "schools": [...], "spagnolo": bool }
    by_site = defaultdict(lambda: {"urls": set(), "schools": [], "spagnolo": False})

    totale_righe = 0
    scartate_no_sito = 0
    incluse_no_spa = 0

    for r in rows:
        totale_righe += 1
        d = {h: r[i] for h, i in idx.items()}

        is_spa = (d.get("SPA/NO SPA") or "").strip().upper() == "SPA"
        if not args.all and not is_spa:
            incluse_no_spa += 1
            continue

        raw_url = (d.get("SITO WEB SCUOLA") or "").strip()
        key = normalize_url_key(raw_url)
        if not key:
            scartate_no_sito += 1
            continue

        entry = by_site[key]
        # ricostruiamo l'URL con schema, per usarlo poi per il fetch
        display_url = raw_url if re.match(r"^https?://", raw_url, re.IGNORECASE) else f"http://{raw_url}"
        entry["urls"].add(display_url)
        entry["spagnolo"] = entry["spagnolo"] or is_spa
        entry["schools"].append({
            "nome_istituto": d.get("NOME ISTITUTO"),
            "nome_scuola": d.get("NOME SCUOLA"),
            "tipo_istruzione": d.get("TIPO ISTRUZIONE"),
            "tipo_istituto_dettagliato": d.get("TIPO ISTITUTO DETTAGLIATO"),
            "codice_istituto": d.get("CODICE ISTITUTO"),
            "codice_meccanografico": d.get("CODICE MECCANOGRAFICO SCUOLA"),
            "provincia": d.get("PROVINCIA"),
            "comune": d.get("COMUNE"),
            "indirizzo": d.get("INDIRIZZO"),
            "telefono": d.get("TELEFONO"),
            "email": d.get("EMAIL"),
            "link_maps": d.get("LINK GOOGLE MAPS"),
            "spagnolo": is_spa,
        })

    output = []
    for key, entry in by_site.items():
        url = pick_display_url(entry["urls"])
        # nome sito: usa il nome del primo istituto collegato
        primo = entry["schools"][0]
        # dedup scuole identiche (stesso codice_meccanografico) che si
        # possono ripetere se piu' righe della stessa sede condividevano
        # lo stesso sito
        viste = set()
        scuole_dedup = []
        for s in entry["schools"]:
            k = (s.get("codice_meccanografico"), s.get("nome_scuola"))
            if k in viste:
                continue
            viste.add(k)
            scuole_dedup.append(s)

        output.append({
            "site_name": primo.get("nome_istituto") or url,
            "url": url,
            "spagnolo": entry["spagnolo"],
            "enabled": True,
            "schools": scuole_dedup,
        })

    output.sort(key=lambda e: e["site_name"] or "")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Righe totali nel file: {totale_righe}")
    if not args.all:
        print(f"Righe escluse perche' non SPA: {incluse_no_spa}")
    print(f"Righe/sedi escluse perche' senza sito web valido: {scartate_no_sito}")
    print(f"Siti unici generati: {len(output)}")
    print(f"Scritto: {args.output}")

if __name__ == "__main__":
    main()
