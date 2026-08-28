import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Header, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel


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
}

SYNC_ORDER = ("clients", "providers", "services", "appointments", "appointment_clients", "payments")
SEQUENCE_TABLES = ("clients", "providers", "services", "appointments", "payments")


class SyncPayload(BaseModel):
    data: dict[str, list[dict[str, Any]]]
    replace_remote: bool = False


app = FastAPI(title="Agenda Clientes API")


def load_env_file():
    env_file = Path(__file__).resolve().parent.parent / ".env.local"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


load_env_file()


def database_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise HTTPException(status_code=500, detail="DATABASE_URL nao configurado.")
    return url


def check_api_key(x_api_key: str | None):
    expected_key = os.getenv("SYNC_API_KEY")
    if expected_key and x_api_key != expected_key:
        raise HTTPException(status_code=401, detail="Chave de sincronizacao invalida.")


def clean_row(table, row):
    columns = TABLE_COLUMNS[table]
    cleaned = {}
    for column in columns:
        value = row.get(column)
        if isinstance(value, float):
            value = Decimal(str(value))
        cleaned[column] = value
    return cleaned


def upsert_rows(cur, table, rows):
    if not rows:
        return 0

    columns = TABLE_COLUMNS[table]
    placeholders = ", ".join(["%s"] * len(columns))
    quoted_columns = ", ".join(columns)
    conflict_columns = "id" if table != "appointment_clients" else "appointment_id, client_id"
    update_columns = [column for column in columns if column not in {"id", "appointment_id", "client_id"}]

    if update_columns:
        assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
        conflict_action = f"DO UPDATE SET {assignments}"
    else:
        conflict_action = "DO NOTHING"

    sql = (
        f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_columns}) {conflict_action}"
    )

    values = []
    for row in rows:
        cleaned = clean_row(table, row)
        values.append(tuple(cleaned[column] for column in columns))
    cur.executemany(sql, values)
    return len(values)


def reset_sequence(cur, table):
    cur.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{table}', 'id'),
            COALESCE((SELECT MAX(id) FROM {table}), 1),
            COALESCE((SELECT MAX(id) FROM {table}), 0) > 0
        )
        """
    )


@app.get("/health")
def health():
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return cur.fetchone()


@app.post("/sync/full")
def sync_full(payload: SyncPayload, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    unknown_tables = sorted(set(payload.data) - set(TABLE_COLUMNS))
    if unknown_tables:
        raise HTTPException(status_code=400, detail=f"Tabelas desconhecidas: {', '.join(unknown_tables)}")

    counts = {}
    with psycopg.connect(database_url()) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET CONSTRAINTS ALL DEFERRED")
                if payload.replace_remote:
                    cur.execute(
                        """
                        TRUNCATE TABLE
                            appointment_clients,
                            payments,
                            appointments,
                            clients,
                            providers,
                            services
                        RESTART IDENTITY CASCADE
                        """
                    )
                for table in SYNC_ORDER:
                    counts[table] = upsert_rows(cur, table, payload.data.get(table, []))
                for table in SEQUENCE_TABLES:
                    reset_sequence(cur, table)
    return {"status": "ok", "synced": counts}


@app.get("/agenda/week")
def agenda_week(start_date: str, end_date: str, provider_id: int | None = None, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    sql = """
        SELECT a.id, a.appointment_date, a.start_time, a.end_time, a.state, a.notes,
               p.name AS provider_name,
               s.type || ' - ' || s.name AS service_name
        FROM appointments a
        JOIN providers p ON p.id = a.provider_id
        JOIN services s ON s.id = a.service_id
        WHERE a.appointment_date BETWEEN %s AND %s
    """
    params: list[Any] = [start_date, end_date]
    if provider_id:
        sql += " AND a.provider_id = %s"
        params.append(provider_id)
    sql += " ORDER BY a.appointment_date, a.start_time"
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {"items": cur.fetchall()}


@app.get("/providers")
def providers(x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name
                FROM providers
                WHERE active = 1
                ORDER BY name
                """
            )
            return {"items": cur.fetchall()}


@app.get("/clients")
def clients(search: str = "", x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    term = f"%{search.strip()}%"
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.name, c.phone, c.email,
                       COALESCE(s.name, '') AS aula_name,
                       COALESCE(p.name, '') AS provider_name,
                       COALESCE(s.price, 0) AS price,
                       c.service_start_date,
                       c.service_end_date
                FROM clients c
                LEFT JOIN services s ON s.id = c.selected_service_id
                LEFT JOIN providers p ON p.id = s.provider_id
                WHERE c.active = 1
                  AND c.name ILIKE %s
                ORDER BY c.name
                LIMIT 100
                """,
                (term,),
            )
            return {"items": cur.fetchall()}


@app.get("/payments/pending")
def pending_payments(x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pay.id, c.name AS client_name,
                       s.type || ' - ' || s.name AS service_name,
                       pay.period_start_date, pay.period_end_date,
                       pay.amount, pay.state
                FROM payments pay
                JOIN clients c ON c.id = pay.client_id
                JOIN services s ON s.id = pay.service_id
                WHERE pay.state IN ('Pendente', 'Parcial')
                ORDER BY c.name, pay.period_start_date
                """
            )
            return {"items": cur.fetchall()}
