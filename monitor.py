"""
Monitor de vacantes Amazon.jobs -> Telegram
Consulta el endpoint JSON interno del buscador de amazon.jobs,
filtra puestos de gerencia/liderazgo, detecta vacantes nuevas,
notifica por Telegram, y se reporta solo (resumen diario + alerta
si algo se rompe) para que no tengas que estar checando manualmente.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

SEARCH_URL = "https://www.amazon.jobs/es/search.json"

SEARCH_PARAMS = {
    "offset": 0,
    "result_limit": 30,
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

# Palabras que si aparecen en el titulo, se descarta la vacante (gerencias,
# posiciones de liderazgo/corporativas). Dejamos pasar todo lo demas.
TITLE_EXCLUDE_KEYWORDS = [
    "manager",
    "gerente",
    "director",
    "supervisor",
    "sr.",
    "senior",
    "lead",
    "líder",
    "lider",
    "jefe",
    "coordinador",
]

# Cuantas veces reintentar la consulta al endpoint de Amazon si falla
# (timeout, error de red, 5xx). Espera progresiva entre intentos.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

# Despues de cuantas corridas fallidas SEGUIDAS se manda una alerta a Telegram.
FAILURE_ALERT_THRESHOLD = 3

# Cada cuantas horas se manda el resumen "sigo vivo" aunque no haya nada nuevo.
DAILY_SUMMARY_INTERVAL_HOURS = 24

SEEN_JOBS_FILE = Path("seen_jobs.json")
HISTORY_FILE = Path("history.json")
STATE_FILE = Path("state.json")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# UTILIDADES DE ARCHIVOS
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# AMAZON
# ---------------------------------------------------------------------------

def fetch_jobs() -> list[dict]:
    """Consulta el endpoint de Amazon con reintentos automaticos."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(SEARCH_URL, params=SEARCH_PARAMS, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            return data.get("jobs", [])
        except Exception as e:
            last_error = e
            print(f"Intento {attempt}/{MAX_RETRIES} falló: {e}")
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS * attempt
                print(f"Reintentando en {wait}s...")
                time.sleep(wait)
    raise RuntimeError(f"No se pudo consultar Amazon tras {MAX_RETRIES} intentos: {last_error}")


def title_is_operational(title: str) -> bool:
    t = title.lower()
    return not any(bad in t for bad in TITLE_EXCLUDE_KEYWORDS)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

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


def build_job_message(job: dict) -> str:
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


def build_daily_summary_message(total_seen: int, new_today: int) -> str:
    fecha = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    return (
        f"📊 <b>Resumen diario del monitor</b> ({fecha})\n\n"
        f"✅ El bot sigue activo y revisando cada 15 min.\n"
        f"🆕 Vacantes nuevas detectadas hoy: {new_today}\n"
        f"📁 Total histórico de vacantes vistas: {total_seen}"
    )


def build_failure_alert_message(consecutive_failures: int, last_error: str) -> str:
    return (
        f"🔴 <b>Alerta: el monitor lleva {consecutive_failures} corridas fallidas seguidas</b>\n\n"
        f"Puede que Amazon haya cambiado el endpoint o esté bloqueando la consulta.\n"
        f"Último error: <code>{last_error}</code>\n\n"
        f"Vale la pena revisar el log de GitHub Actions."
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    state = load_json(STATE_FILE, {
        "consecutive_failures": 0,
        "last_failure_alert_sent": None,
        "last_daily_summary": None,
        "new_jobs_since_last_summary": 0,
    })

    # --- Intentar consultar Amazon ---
    try:
        jobs = fetch_jobs()
    except Exception as e:
        print(f"ERROR FINAL: {e}")
        state["consecutive_failures"] += 1
        print(f"Fallas consecutivas: {state['consecutive_failures']}")

        if state["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD:
            # Evita mandar la alerta en cada corrida una vez cruzado el umbral;
            # solo la manda la primera vez que se cruza.
            if state["consecutive_failures"] == FAILURE_ALERT_THRESHOLD:
                try:
                    send_telegram_message(build_failure_alert_message(state["consecutive_failures"], str(e)))
                except Exception as telegram_error:
                    print(f"Ademas fallo el aviso a Telegram: {telegram_error}")

        save_json(STATE_FILE, state)
        sys.exit(1)

    # Si llegamos aqui, la consulta funciono: resetea el contador de fallas
    if state["consecutive_failures"] > 0:
        print(f"Recuperado tras {state['consecutive_failures']} fallas.")
    state["consecutive_failures"] = 0

    print(f"Total vacantes recibidas del endpoint: {len(jobs)}")

    operational_jobs = [j for j in jobs if title_is_operational(j.get("title", ""))]
    print(f"Vacantes tras excluir gerencia/liderazgo: {len(operational_jobs)}")

    seen_ids = set(load_json(SEEN_JOBS_FILE, []))
    history = load_json(HISTORY_FILE, [])

    new_jobs = [j for j in operational_jobs if j.get("id_icims") not in seen_ids]
    print(f"Vacantes nuevas encontradas: {len(new_jobs)}")

    for job in new_jobs:
        msg = build_job_message(job)
        send_telegram_message(msg)

        job_id = job.get("id_icims")
        seen_ids.add(job_id)
        history.append({
            "id_icims": job_id,
            "title": job.get("title"),
            "location": job.get("location"),
            "posted_date": job.get("posted_date"),
            "job_path": job.get("job_path"),
            "detected_at": now_iso(),
        })

    if new_jobs:
        save_json(SEEN_JOBS_FILE, sorted(seen_ids))
        save_json(HISTORY_FILE, history)

    state["new_jobs_since_last_summary"] = state.get("new_jobs_since_last_summary", 0) + len(new_jobs)

    # --- Resumen diario ---
    last_summary = state.get("last_daily_summary")
    should_send_summary = True
    if last_summary:
        elapsed_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(last_summary)).total_seconds() / 3600
        should_send_summary = elapsed_hours >= DAILY_SUMMARY_INTERVAL_HOURS

    if should_send_summary:
        send_telegram_message(
            build_daily_summary_message(
                total_seen=len(seen_ids),
                new_today=state["new_jobs_since_last_summary"],
            )
        )
        state["last_daily_summary"] = now_iso()
        state["new_jobs_since_last_summary"] = 0

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()