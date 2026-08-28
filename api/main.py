import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
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


WEEK_DAYS = ("Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo")


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


def week_start_for(day: date):
    return day - timedelta(days=day.weekday())


def parse_mobile_date(value: str | None):
    if not value:
        return week_start_for(date.today())
    try:
        return week_start_for(datetime.strptime(value, "%Y-%m-%d").date())
    except ValueError:
        return week_start_for(date.today())


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


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!doctype html>
    <html lang="pt">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="0; url=/mobile">
        <title>Agenda</title>
      </head>
      <body>
        <a href="/mobile">Abrir agenda mobile</a>
      </body>
    </html>
    """


@app.get("/mobile", response_class=HTMLResponse)
def mobile(week: str | None = None):
    start = parse_mobile_date(week)
    end = start + timedelta(days=6)
    previous_week = (start - timedelta(days=7)).isoformat()
    next_week = (start + timedelta(days=7)).isoformat()

    rows_by_day = {day.isoformat(): [] for day in (start + timedelta(days=index) for index in range(7))}
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.appointment_date, a.start_time, a.end_time, a.state,
                       p.name AS provider_name,
                       s.type || ' - ' || s.name AS service_name
                FROM appointments a
                JOIN providers p ON p.id = a.provider_id
                JOIN services s ON s.id = a.service_id
                WHERE a.appointment_date BETWEEN %s AND %s
                ORDER BY a.appointment_date, a.start_time
                """,
                (start.isoformat(), end.isoformat()),
            )
            for row in cur.fetchall():
                rows_by_day.setdefault(row["appointment_date"], []).append(row)

    day_sections = []
    for index, day_name in enumerate(WEEK_DAYS):
        current_day = start + timedelta(days=index)
        rows = rows_by_day.get(current_day.isoformat(), [])
        if rows:
            cards = "\n".join(
                f"""
                <article class="card state-{escape(row['state'].lower())}">
                  <div class="time">{escape(row['start_time'])} - {escape(row['end_time'])}</div>
                  <div class="service">{escape(row['service_name'])}</div>
                  <div class="provider">{escape(row['provider_name'])}</div>
                  <span class="badge">{escape(row['state'])}</span>
                </article>
                """
                for row in rows
            )
        else:
            cards = '<p class="empty">Sem marcacoes.</p>'
        day_sections.append(
            f"""
            <section class="day">
              <h2>{day_name} <span>{current_day:%d/%m/%Y}</span></h2>
              {cards}
            </section>
            """
        )

    return f"""
    <!doctype html>
    <html lang="pt">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Agenda Mobile</title>
        <style>
          :root {{
            color-scheme: light;
            --bg: #f3f6fb;
            --surface: #ffffff;
            --text: #1f2937;
            --muted: #64748b;
            --primary: #0f766e;
            --border: #d9e2ee;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: "Segoe UI", Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
          }}
          header {{
            position: sticky;
            top: 0;
            z-index: 2;
            padding: 16px;
            background: var(--primary);
            color: white;
            box-shadow: 0 2px 8px rgba(15, 23, 42, .18);
          }}
          h1 {{
            margin: 0;
            font-size: 22px;
            font-weight: 700;
          }}
          .range {{
            margin-top: 4px;
            color: #d1fae5;
            font-size: 14px;
          }}
          nav {{
            display: flex;
            gap: 8px;
            padding: 12px 16px;
            background: #e8f4f2;
            border-bottom: 1px solid var(--border);
          }}
          nav a {{
            flex: 1;
            text-align: center;
            padding: 10px 8px;
            border-radius: 8px;
            background: white;
            color: var(--primary);
            text-decoration: none;
            font-weight: 700;
            border: 1px solid #b7d9d4;
          }}
          main {{
            padding: 12px;
            max-width: 780px;
            margin: 0 auto;
          }}
          .day {{
            margin-bottom: 14px;
          }}
          h2 {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin: 10px 2px 8px;
            font-size: 16px;
          }}
          h2 span {{
            color: var(--muted);
            font-size: 13px;
            font-weight: 500;
          }}
          .card {{
            position: relative;
            margin-bottom: 8px;
            padding: 12px 14px;
            border: 1px solid var(--border);
            border-left: 5px solid var(--primary);
            border-radius: 8px;
            background: var(--surface);
            box-shadow: 0 1px 3px rgba(15, 23, 42, .08);
          }}
          .time {{
            font-size: 14px;
            color: var(--primary);
            font-weight: 800;
          }}
          .service {{
            margin-top: 4px;
            padding-right: 92px;
            font-size: 15px;
            font-weight: 700;
          }}
          .provider {{
            margin-top: 3px;
            color: var(--muted);
            font-size: 13px;
          }}
          .badge {{
            position: absolute;
            top: 12px;
            right: 12px;
            padding: 4px 7px;
            border-radius: 999px;
            background: #ccfbf1;
            color: #134e4a;
            font-size: 11px;
            font-weight: 800;
          }}
          .state-cancelado {{
            border-left-color: #be123c;
            background: #fff1f2;
          }}
          .state-concluido {{
            border-left-color: #15803d;
            background: #f0fdf4;
          }}
          .empty {{
            margin: 0 0 8px;
            padding: 12px 14px;
            border: 1px dashed var(--border);
            border-radius: 8px;
            color: var(--muted);
            background: rgba(255, 255, 255, .6);
          }}
        </style>
      </head>
      <body>
        <header>
          <h1>Agenda</h1>
          <div class="range">Semana de {start:%d/%m/%Y} a {end:%d/%m/%Y}</div>
        </header>
        <nav>
          <a href="/mobile?week={previous_week}">Semana anterior</a>
          <a href="/mobile">Hoje</a>
          <a href="/mobile?week={next_week}">Proxima semana</a>
        </nav>
        <main>
          {''.join(day_sections)}
        </main>
      </body>
    </html>
    """


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
