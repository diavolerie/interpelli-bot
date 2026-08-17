"""
Svuota in modo sicuro alcune liste "notified" in state.json,
senza toccare "evaluated" ne' il resto del file.

Uso:
    py fix_state.py

Deve essere lanciato nella stessa cartella dove si trova state.json
(quella del tuo repository). Crea anche una copia di backup
state.json.bak prima di modificare, per sicurezza.
"""

import json
import shutil
from pathlib import Path

STATE_PATH = Path("state.json")

# Province per cui azzerare la lista "notified" (annunci trovati ma
# non recapitati su Telegram per il token mancante)
SITES_TO_RESET = [
    "USR Lombardia - Lodi",
    "USR Lombardia - Mantova",
    "USR Lombardia - Monza e Brianza",
    "USR Lombardia - Varese",
]

def main():
    if not STATE_PATH.exists():
        print("ERRORE: state.json non trovato in questa cartella.")
        return

    # backup di sicurezza
    backup_path = STATE_PATH.with_suffix(".json.bak")
    shutil.copy(STATE_PATH, backup_path)
    print(f"Backup creato: {backup_path}")

    with open(STATE_PATH, "r", encoding="utf-8") as f:
        state = json.load(f)

    notified = state.get("notified", {})

    for site in SITES_TO_RESET:
        before = notified.get(site, [])
        if before:
            print(f"'{site}': svuoto {len(before)} id -> {before}")
        else:
            print(f"'{site}': gia' vuoto, nessuna modifica.")
        notified[site] = []

    state["notified"] = notified

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print("\nFatto. state.json aggiornato.")
    print("Se qualcosa fosse andato storto, ripristina da state.json.bak")

if __name__ == "__main__":
    main()
