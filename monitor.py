"""
Monitor de vacantes Amazon.jobs -> Telegram
Consulta el endpoint JSON interno del buscador de amazon.jobs,
filtra por keywords en el titulo, detecta vacantes nuevas
comparando contra un archivo local, y notifica por Telegram.
"""

import json
import os
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

# URL base del endpoint JSON. Ajusta los query params a los filtros de Sonia
# (ubicacion, categoria, radio, etc). Estos vienen de tu URL personalizada,
# solo cambiando /search por /search.json
SEARCH_URL = "https://www.amazon.jobs/es/search.json"

SEARCH_PARAMS = {
    "offset": 0,
    "result_limit": 30,          # pide mas de 10 para no perder vacantes nuevas si hay varias a la vez
    "sort": "relevant",
    "category[]": "fulfillment-warehouse-associate",
    "state[]": "Mexico",
    "city[]": "Tepotzotlán",
    "distanceType": "Mi",
    "radius": "24km",
    "latitude": 19.71323,
    "longitude": -99.2196,
    "loc_query": "Tepotzotlán, Edomex, México",
    "city": "Tepotzotlán",
    "country": "MEX",
    "region": "México",
}

SEEN_JOBS_FILE = Path("seen_jobs.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# LOGICA
# ---------------------------------------------------------------------------

def fetch_jobs() -> list[dict]:
    resp = requests.get(SEARCH_URL, params=SEARCH_PARAMS, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("jobs", [])


def load_seen_ids() -> set[str]:
    if not SEEN_JOBS_FILE.exists():
        return set()
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen_ids(ids: set[str]) -> None:
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def send_telegram_message(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en variables de entorno.")
        sys.exit(1)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=20)
    resp.raise_for_status()


def build_message(job: dict) -> str:
    title = job.get("title", "Sin título")
    location = job.get("location", "Sin ubicación")
    posted = job.get("posted_date", "?")
    job_path = job.get("job_path", "")
    link = f"https://www.amazon.jobs{job_path}" if job_path else "https://www.amazon.jobs/es"

    return (
        f"🟢 <b>Nueva vacante Amazon</b>\n\n"
        f"<b>{title}</b>\n"
        f"📍 {location}\n"
        f"🗓 Publicada: {posted}\n"
        f"🔗 <a href=\"{link}\">Aplicar aquí</a>"
    )


def main() -> None:
    jobs = fetch_jobs()
    print(f"Total vacantes recibidas del endpoint: {len(jobs)}")

    seen_ids = load_seen_ids()
    new_jobs = [j for j in jobs if j.get("id_icims") not in seen_ids]

    if not new_jobs:
        print("No hay vacantes nuevas.")
        return

    print(f"Vacantes nuevas encontradas: {len(new_jobs)}")

    for job in new_jobs:
        msg = build_message(job)
        send_telegram_message(msg)
        seen_ids.add(job.get("id_icims"))

    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()