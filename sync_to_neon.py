import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
DB_FILE = APP_DIR / "agenda.db"
ENV_FILE = APP_DIR / ".env.local"
DEFAULT_API_URL = "http://127.0.0.1:8000"

TABLE_COLUMNS = {
    "clients": (
        "id",
        "name",
        "phone",
        "email",
        "notes",
        "selected_service_id",
        "service_start_date",
        "service_end_date",
        "active",
        "created_at",
    ),
    "providers": (
        "id",
        "name",
        "phone",
        "email",
        "notes",
        "selected_service_id",
        "active",
        "created_at",
    ),
    "services": (
        "id",
        "type",
        "name",
        "provider_id",
        "price",
        "notes",
        "active",
        "created_at",
    ),
    "appointments": (
        "id",
        "appointment_date",
        "start_time",
        "end_time",
        "client_id",
        "provider_id",
        "service_id",
        "state",
        "notes",
        "created_at",
    ),
    "appointment_clients": ("appointment_id", "client_id"),
    "payments": (
        "id",
        "client_id",
        "service_id",
        "period_start_date",
        "period_end_date",
        "amount",
        "state",
        "payment_date",
        "notes",
        "created_at",
    ),
    "users": (
        "id",
        "username",
        "password_hash",
        "full_name",
        "can_clients",
        "can_providers",
        "can_services",
        "can_agenda",
        "can_payments",
        "can_backup",
        "can_sync",
        "can_users",
        "active",
        "created_at",
    ),
}

COLUMN_DEFAULTS = {
    "phone": "",
    "email": "",
    "notes": "",
    "service_start_date": "",
    "service_end_date": "",
    "active": 1,
    "provider_id": None,
    "selected_service_id": None,
    "price": None,
    "state": "",
    "payment_date": "",
    "period_start_date": "",
    "period_end_date": "",
    "amount": 0,
    "full_name": "",
    "can_clients": 0,
    "can_providers": 0,
    "can_services": 0,
    "can_agenda": 0,
    "can_payments": 0,
    "can_backup": 0,
    "can_sync": 0,
    "can_users": 0,
}


def load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def read_table(conn, table, columns):
    if not table_exists(conn, table):
        return []
    existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    readable_columns = [column for column in columns if column in existing_columns]
    selected_columns = ", ".join(readable_columns)
    rows = conn.execute(f"SELECT {selected_columns} FROM {table}").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for column in columns:
            if column not in item:
                item[column] = COLUMN_DEFAULTS.get(column)
        result.append(item)
    return result


def build_payload():
    if not DB_FILE.exists():
        raise FileNotFoundError(f"Base SQLite nao encontrada: {DB_FILE}")
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "data": {
                table: read_table(conn, table, columns)
                for table, columns in TABLE_COLUMNS.items()
            }
        }
    finally:
        conn.close()


def send_payload(payload):
    api_url = os.getenv("AGENDA_API_URL", DEFAULT_API_URL).rstrip("/")
    sync_key = os.getenv("SYNC_API_KEY", "")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if sync_key:
        headers["X-API-Key"] = sync_key
    request = Request(f"{api_url}/sync/full", data=body, headers=headers, method="POST")
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    load_env_file()
    try:
        payload = build_payload()
        result = send_payload(payload)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    except HTTPError as exc:
        print(f"Erro HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
        return 1
    except URLError as exc:
        print(f"Nao foi possivel contactar a API: {exc.reason}")
        return 1

    print("Sincronizacao concluida.")
    for table, count in result.get("synced", {}).items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
