import hashlib
import secrets
import shutil
import sqlite3
import sys
import threading
from ctypes import windll
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import END, BooleanVar, Canvas, Menu, StringVar, Tk, Toplevel, filedialog, messagebox, ttk

try:
    import sync_to_neon
except ImportError:
    from . import sync_to_neon


APP_TITLE = "Gestao e Agenda"
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
DB_FILE = APP_DIR / "agenda.db"
SCHEMA_VERSION = 5
SERVICE_TYPES = ("Aula", "Terapia", "Servico")
APPOINTMENT_STATES = ("Marcado", "Concluido", "Cancelado")
PAYMENT_STATES = ("Pendente", "Pago", "Parcial", "Cancelado")
USER_PERMISSIONS = (
    ("can_clients", "Clientes"),
    ("can_providers", "Prestadores"),
    ("can_services", "Servicos"),
    ("can_agenda", "Marcar Consulta"),
    ("can_payments", "Pagamentos"),
    ("can_backup", "Backup"),
    ("can_sync", "Sincronizar Neon"),
    ("can_users", "Utilizadores"),
)
WEEK_DAYS = ("Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo")
BG_COLOR = "#f3f6fb"
SURFACE_COLOR = "#ffffff"
SURFACE_ALT_COLOR = "#f8fafc"
TEXT_COLOR = "#1f2937"
MUTED_COLOR = "#64748b"
PRIMARY_COLOR = "#0f766e"
PRIMARY_HOVER_COLOR = "#115e59"
ACCENT_COLOR = "#db2777"
BORDER_COLOR = "#cbd5e1"


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200000).hex()
    return f"{salt}${password_hash}"


def verify_password(password, stored_hash):
    if not stored_hash or "$" not in stored_hash:
        return False
    salt, expected_hash = stored_hash.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), f"{salt}${expected_hash}")


def connect_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    db_existed = DB_FILE.exists()
    if db_existed and database_needs_migration():
        backup_database_before_migration()

    conn = connect_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                selected_service_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (selected_service_id) REFERENCES services(id)
            );

            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                provider_id INTEGER,
                price REAL,
                notes TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (provider_id) REFERENCES providers(id)
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                client_id INTEGER NOT NULL,
                provider_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'Marcado',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id),
                FOREIGN KEY (provider_id) REFERENCES providers(id),
                FOREIGN KEY (service_id) REFERENCES services(id)
            );

            CREATE TABLE IF NOT EXISTS appointment_clients (
                appointment_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                PRIMARY KEY (appointment_id, client_id),
                FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
                FOREIGN KEY (client_id) REFERENCES clients(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                period_start_date TEXT NOT NULL DEFAULT '',
                period_end_date TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'Pendente',
                payment_date TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES clients(id),
                FOREIGN KEY (service_id) REFERENCES services(id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL DEFAULT '',
                can_clients INTEGER NOT NULL DEFAULT 0,
                can_providers INTEGER NOT NULL DEFAULT 0,
                can_services INTEGER NOT NULL DEFAULT 0,
                can_agenda INTEGER NOT NULL DEFAULT 0,
                can_payments INTEGER NOT NULL DEFAULT 0,
                can_backup INTEGER NOT NULL DEFAULT 0,
                can_sync INTEGER NOT NULL DEFAULT 0,
                can_users INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO appointment_clients (appointment_id, client_id)
            SELECT id, client_id
            FROM appointments
            WHERE client_id IS NOT NULL;
            """
        )
        ensure_column(conn, "clients", "selected_service_id", "INTEGER")
        ensure_column(conn, "clients", "service_start_date", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "clients", "service_end_date", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "providers", "selected_service_id", "INTEGER")
        ensure_column(conn, "services", "provider_id", "INTEGER")
        ensure_default_admin_user(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def database_needs_migration():
    conn = sqlite3.connect(DB_FILE)
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < SCHEMA_VERSION:
            return True
        expected_columns = {
            "clients": {"selected_service_id", "service_start_date", "service_end_date"},
            "providers": {"selected_service_id"},
            "services": {"provider_id"},
        }
        for table, columns in expected_columns.items():
            existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not columns.issubset(existing_columns):
                return True
        for table_name in ("appointment_clients", "payments", "users"):
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            if table is None:
                return True
        return False
    finally:
        conn.close()


def ensure_default_admin_user(conn):
    row = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()
    if row["total"]:
        return
    values = {
        "username": "admin",
        "password_hash": hash_password("admin"),
        "full_name": "Administrador",
        "active": 1,
    }
    for key, _label in USER_PERMISSIONS:
        values[key] = 1
    keys = list(values.keys())
    placeholders = ", ".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO users ({', '.join(keys)}) VALUES ({placeholders})",
        [values[key] for key in keys],
    )


def backup_database_before_migration():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = DB_FILE.with_name(f"agenda_backup_pre_migration_{timestamp}.db")
    shutil.copy2(DB_FILE, backup_path)


def ensure_column(conn, table, column, definition):
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def query_all(sql, params=()):
    conn = connect_db()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def execute(sql, params=()):
    conn = connect_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def replace_appointment_clients(appointment_id, client_ids):
    conn = connect_db()
    try:
        conn.execute("DELETE FROM appointment_clients WHERE appointment_id = ?", (appointment_id,))
        conn.executemany(
            "INSERT INTO appointment_clients (appointment_id, client_id) VALUES (?, ?)",
            [(appointment_id, client_id) for client_id in client_ids],
        )
        conn.commit()
    finally:
        conn.close()


def ensure_default_client_id():
    rows = query_all("SELECT id FROM clients WHERE name = ? ORDER BY id LIMIT 1", ("Sem cliente",))
    if rows:
        return rows[0]["id"]
    return execute(
        "INSERT INTO clients (name, notes, active) VALUES (?, ?, 0)",
        ("Sem cliente", "Registo interno usado por marcacoes sem cliente definido."),
    )


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_time(value):
    return datetime.strptime(value, "%H:%M").time()


def minutes_from_time(value):
    parsed = parse_time(value)
    return parsed.hour * 60 + parsed.minute


def format_minutes(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_currency(value):
    return f"{value:.2f}"


def validate_time_range(start_value, end_value):
    start = parse_time(start_value)
    end = parse_time(end_value)
    if start >= end:
        raise ValueError("A hora final deve ser posterior a hora inicial.")


def week_start_for(day):
    return day - timedelta(days=day.weekday())


def clamp_week_to_year(day, year):
    if day.year == year:
        return week_start_for(day)
    return week_start_for(date(year, 1, 1))


def date_options():
    today = date.today()
    start = today - timedelta(days=365)
    return [(start + timedelta(days=index)).isoformat() for index in range(365 * 3 + 1)]


class EntityTab(ttk.Frame):
    def __init__(self, parent, table, title, fields, reload_callback=None):
        super().__init__(parent, padding=16)
        self.table = table
        self.title = title
        self.fields = fields
        self.reload_callback = reload_callback
        self.selected_id = None
        self.vars = {}
        self.active_var = BooleanVar(value=True)
        self.search_var = StringVar()
        self.provider_options = {}
        self.service_options = {}
        self.client_aula_options = {}
        self.client_service_var = StringVar()
        self.client_provider_var = StringVar()
        self.client_price_var = StringVar()
        self.client_start_date_var = StringVar()
        self.client_end_date_var = StringVar()

        self.build()
        self.load_rows()

    def build(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(self, text=self.title, padding=12)
        form.grid(row=0, column=0, columnspan=2, sticky="ew")
        form.columnconfigure(1, weight=1)

        row = 0
        for key, label, kind in self.fields:
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if kind == "combo":
                var = StringVar(value=SERVICE_TYPES[0])
                widget = ttk.Combobox(form, textvariable=var, values=SERVICE_TYPES, state="readonly")
            elif kind == "provider_combo":
                var = StringVar()
                widget = ttk.Combobox(form, textvariable=var, state="readonly")
                self.provider_combo = widget
            elif kind == "service_combo":
                var = StringVar()
                widget = ttk.Combobox(form, textvariable=var, state="readonly")
                self.service_combo = widget
            elif kind == "notes":
                var = None
                widget = ttk.Entry(form)
            else:
                var = StringVar()
                widget = ttk.Entry(form, textvariable=var)
            widget.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
            self.vars[key] = (var, widget, kind)
            row += 1

        ttk.Checkbutton(form, text="Ativo", variable=self.active_var).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        if self.table == "clients":
            self.build_client_service_fields(form, row)
        if self.table == "providers":
            self.reload_service_options()
        if self.table == "services":
            self.reload_provider_options()

        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 8))
        ttk.Button(actions, text="Novo", command=self.clear_form).pack(side="left")
        ttk.Button(actions, text="Guardar", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Inativar", command=self.deactivate).pack(side="left", padx=(8, 0))
        if self.table in {"clients", "providers"}:
            ttk.Button(actions, text="Remover", command=self.delete_selected, style="Danger.TButton").pack(
                side="left",
                padx=(8, 0),
            )
        if self.table == "clients":
            ttk.Button(actions, text="Listagem valores", command=self.open_client_values_report).pack(
                side="left",
                padx=(8, 0),
            )

        ttk.Label(actions, text="Pesquisar").pack(side="left", padx=(24, 6))
        search = ttk.Entry(actions, textvariable=self.search_var, width=28)
        search.pack(side="left")
        search.bind("<KeyRelease>", lambda _event: self.load_rows())

        display_fields = self.display_fields()
        columns = [field[0] for field in display_fields] + ["active"]
        tree_height = 9 if self.table == "clients" else 12
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=tree_height)
        for key, label, _kind in display_fields:
            self.tree.heading(key, text=label)
            self.tree.column(key, minwidth=90, width=self.column_width(key), stretch=False)
        self.tree.heading("active", text="Ativo")
        self.tree.column("active", width=70, anchor="center", stretch=False)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal_scrollbar.grid(row=3, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        self.tree.bind("<Shift-MouseWheel>", self.on_tree_shift_mousewheel)

    def scrollable_width(self):
        tree_width = sum(self.column_width(field[0]) for field in self.display_fields()) + 110
        form_width = 620
        return max(tree_width, form_width)

    def on_tree_shift_mousewheel(self, event):
        self.tree.xview_scroll(int(-1 * (event.delta / 120)), "units")

    def open_client_values_report(self):
        if self.table != "clients":
            return
        window = Toplevel(self.winfo_toplevel())
        window.title("Listagem de clientes e valores")
        window.geometry("900x460")
        window.minsize(760, 360)
        window.configure(bg=BG_COLOR)
        ClientValuesReport(window).pack(fill="both", expand=True)

    def display_fields(self):
        if self.table != "clients":
            if self.table == "providers":
                return [field for field in self.fields if field[0] != "selected_service_id"] + [
                    ("service_name", "Servico", "display")
                ]
            return self.fields
        return self.fields + [
            ("aula_name", "Aula", "display"),
            ("aula_provider", "Prestador aula", "display"),
            ("aula_price", "Valor", "display"),
            ("service_start_date", "Data inicio", "display"),
            ("service_end_date", "Data fim", "display"),
        ]

    def column_width(self, key):
        widths = {
            "name": 180,
            "phone": 115,
            "email": 190,
            "notes": 220,
            "type": 95,
            "provider_id": 170,
            "selected_service_id": 170,
            "service_name": 180,
            "price": 90,
            "aula_name": 170,
            "aula_provider": 170,
            "aula_price": 90,
            "service_start_date": 110,
            "service_end_date": 110,
        }
        return widths.get(key, 150)

    def build_client_service_fields(self, form, row):
        ttk.Label(form, text="Aula").grid(row=row, column=0, sticky="w", pady=4)
        self.client_service_combo = ttk.Combobox(
            form,
            textvariable=self.client_service_var,
            state="readonly",
        )
        self.client_service_combo.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
        self.client_service_combo.bind("<<ComboboxSelected>>", self.on_client_service_selected)

        ttk.Label(form, text="Prestador").grid(row=row + 1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.client_provider_var, state="readonly").grid(
            row=row + 1,
            column=1,
            sticky="ew",
            padx=(10, 0),
            pady=4,
        )

        ttk.Label(form, text="Valor").grid(row=row + 2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.client_price_var, state="readonly").grid(
            row=row + 2,
            column=1,
            sticky="ew",
            padx=(10, 0),
            pady=4,
        )

        ttk.Label(form, text="Data inicio").grid(row=row + 3, column=0, sticky="w", pady=4)
        self.client_start_combo = ttk.Combobox(
            form,
            textvariable=self.client_start_date_var,
            values=date_options(),
            width=12,
        )
        self.client_start_combo.grid(row=row + 3, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(form, text="Data fim").grid(row=row + 4, column=0, sticky="w", pady=4)
        self.client_end_combo = ttk.Combobox(
            form,
            textvariable=self.client_end_date_var,
            values=date_options(),
            width=12,
        )
        self.client_end_combo.grid(row=row + 4, column=1, sticky="w", padx=(10, 0), pady=4)
        self.reload_client_aula_options()

    def clear_form(self):
        self.selected_id = None
        for key, (var, widget, kind) in self.vars.items():
            if kind == "combo":
                var.set(SERVICE_TYPES[0])
            elif kind == "provider_combo":
                var.set("")
            elif kind == "service_combo":
                var.set("")
            else:
                widget.delete(0, END)
        self.active_var.set(True)
        self.clear_client_service_fields()
        self.tree.selection_remove(self.tree.selection())

    def clear_client_service_fields(self):
        if self.table != "clients":
            return
        self.client_service_var.set("")
        self.client_provider_var.set("")
        self.client_price_var.set("")
        self.client_start_date_var.set("")
        self.client_end_date_var.set("")

    def row_values(self):
        values = {}
        for key, (var, widget, kind) in self.vars.items():
            if kind == "combo":
                values[key] = var.get().strip()
            elif kind == "provider_combo":
                values[key] = self.provider_options.get(var.get().strip())
            elif kind == "service_combo":
                values[key] = self.service_options.get(var.get().strip())
            else:
                values[key] = widget.get().strip()
        values["active"] = 1 if self.active_var.get() else 0
        if self.table == "clients":
            aula = self.client_aula_options.get(self.client_service_var.get().strip())
            values["selected_service_id"] = aula["id"] if aula else None
            values["service_start_date"] = self.client_start_date_var.get().strip()
            values["service_end_date"] = self.client_end_date_var.get().strip()
        return values

    def save(self):
        values = self.row_values()
        if not values.get("name"):
            messagebox.showwarning("Campo obrigatorio", "Indique o nome.")
            return

        if self.table == "services":
            if values["type"] not in SERVICE_TYPES:
                messagebox.showwarning("Tipo invalido", "Escolha um tipo valido.")
                return
            if values.get("price"):
                try:
                    values["price"] = float(values["price"].replace(",", "."))
                except ValueError:
                    messagebox.showwarning("Preco invalido", "Indique um preco numerico ou deixe vazio.")
                    return
            else:
                values["price"] = None
        if self.table == "clients":
            for key, label in (("service_start_date", "Data inicio"), ("service_end_date", "Data fim")):
                if values.get(key):
                    try:
                        parse_date(values[key])
                    except ValueError:
                        messagebox.showwarning("Data invalida", f"Indique uma data valida em {label}: AAAA-MM-DD.")
                        return

        keys = list(values.keys())
        if self.selected_id:
            assignments = ", ".join(f"{key} = ?" for key in keys)
            execute(
                f"UPDATE {self.table} SET {assignments} WHERE id = ?",
                [values[key] for key in keys] + [self.selected_id],
            )
        else:
            placeholders = ", ".join("?" for _ in keys)
            execute(
                f"INSERT INTO {self.table} ({', '.join(keys)}) VALUES ({placeholders})",
                [values[key] for key in keys],
            )

        self.load_rows()
        self.clear_form()
        self.notify_reload()

    def deactivate(self):
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha um registo para inativar.")
            return
        execute(f"UPDATE {self.table} SET active = 0 WHERE id = ?", (self.selected_id,))
        self.load_rows()
        self.clear_form()
        self.notify_reload()

    def delete_selected(self):
        if self.table not in {"clients", "providers"}:
            return
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha um registo para remover.")
            return
        label = "cliente" if self.table == "clients" else "prestador"
        if not messagebox.askyesno(
            "Confirmar remocao",
            f"Tem a certeza que pretende remover definitivamente este {label}?",
        ):
            return
        if self.table == "clients":
            self.delete_client()
        else:
            self.delete_provider()
        self.load_rows()
        self.clear_form()
        self.notify_reload()

    def delete_client(self):
        replacement_client_id = ensure_default_client_id()
        conn = connect_db()
        try:
            conn.execute("DELETE FROM appointment_clients WHERE client_id = ?", (self.selected_id,))
            conn.execute("DELETE FROM payments WHERE client_id = ?", (self.selected_id,))
            if self.selected_id != replacement_client_id:
                conn.execute(
                    "UPDATE appointments SET client_id = ? WHERE client_id = ?",
                    (replacement_client_id, self.selected_id),
                )
            conn.execute("DELETE FROM clients WHERE id = ?", (self.selected_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_provider(self):
        conn = connect_db()
        try:
            conn.execute(
                """
                DELETE FROM appointment_clients
                WHERE appointment_id IN (
                    SELECT id FROM appointments WHERE provider_id = ?
                )
                """,
                (self.selected_id,),
            )
            conn.execute("DELETE FROM appointments WHERE provider_id = ?", (self.selected_id,))
            conn.execute("UPDATE services SET provider_id = NULL WHERE provider_id = ?", (self.selected_id,))
            conn.execute("DELETE FROM providers WHERE id = ?", (self.selected_id,))
            conn.commit()
        finally:
            conn.close()

    def load_rows(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        term = f"%{self.search_var.get().strip()}%"
        if self.table == "services":
            rows = query_all(
                """
                SELECT s.id, s.type, s.name, COALESCE(p.name, '') AS provider_id,
                       COALESCE(s.price, '') AS price, s.notes, s.active
                FROM services s
                LEFT JOIN providers p ON p.id = s.provider_id
                WHERE s.name LIKE ? OR s.type LIKE ? OR s.notes LIKE ? OR COALESCE(p.name, '') LIKE ?
                ORDER BY s.active DESC, s.type, s.name
                """,
                (term, term, term, term),
            )
        elif self.table == "providers":
            rows = query_all(
                """
                SELECT p.id, p.name, p.phone, p.email, p.notes, p.selected_service_id,
                       COALESCE(s.type || ' - ' || s.name, '') AS service_name,
                       p.active
                FROM providers p
                LEFT JOIN services s ON s.id = p.selected_service_id
                WHERE p.name LIKE ?
                   OR p.phone LIKE ?
                   OR p.email LIKE ?
                   OR p.notes LIKE ?
                   OR COALESCE(s.name, '') LIKE ?
                   OR COALESCE(s.type, '') LIKE ?
                ORDER BY p.active DESC, p.name
                """,
                (term, term, term, term, term, term),
            )
        elif self.table == "clients":
            rows = query_all(
                """
                SELECT c.id, c.name, c.phone, c.email, c.notes, c.active,
                       COALESCE(s.name, '') AS aula_name,
                       COALESCE(p.name, '') AS aula_provider,
                       COALESCE(s.price, '') AS aula_price,
                       c.service_start_date,
                       c.service_end_date
                FROM clients c
                LEFT JOIN services s ON s.id = c.selected_service_id
                LEFT JOIN providers p ON p.id = s.provider_id
                WHERE c.name LIKE ?
                   OR c.phone LIKE ?
                   OR c.email LIKE ?
                   OR c.notes LIKE ?
                   OR COALESCE(s.name, '') LIKE ?
                   OR COALESCE(p.name, '') LIKE ?
                ORDER BY c.active DESC, c.name
                """,
                (term, term, term, term, term, term),
            )
        else:
            rows = query_all(
                f"""
                SELECT id, name, phone, email, notes, active
                FROM {self.table}
                WHERE name LIKE ? OR phone LIKE ? OR email LIKE ? OR notes LIKE ?
                ORDER BY active DESC, name
                """,
                (term, term, term, term),
            )

        for row in rows:
            values = [self.display_value(row, field[0]) for field in self.display_fields()]
            values.append("Sim" if row["active"] else "Nao")
            self.tree.insert("", END, iid=str(row["id"]), values=values)

    def display_value(self, row, key):
        value = row[key]
        if key in {"price", "aula_price"} and value != "":
            return f"{float(value):.2f}"
        return value

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        row = query_all(f"SELECT * FROM {self.table} WHERE id = ?", (self.selected_id,))[0]
        for key, (_var, widget, kind) in self.vars.items():
            if kind == "combo":
                self.vars[key][0].set(row[key])
            elif kind == "provider_combo":
                self.vars[key][0].set(self.provider_name_by_id(row[key]))
            elif kind == "service_combo":
                self.vars[key][0].set(self.service_name_by_id(row[key]))
            else:
                widget.delete(0, END)
                widget.insert(0, "" if row[key] is None else str(row[key]))
        self.active_var.set(bool(row["active"]))
        if self.table == "clients":
            self.load_client_service_fields(row)

    def notify_reload(self):
        if self.reload_callback:
            self.reload_callback()

    def reload_provider_options(self):
        providers = query_all("SELECT id, name FROM providers WHERE active = 1 ORDER BY name")
        self.provider_options = {row["name"]: row["id"] for row in providers}
        if hasattr(self, "provider_combo"):
            self.provider_combo["values"] = list(self.provider_options.keys())

    def provider_name_by_id(self, provider_id):
        if not provider_id:
            return ""
        rows = query_all("SELECT name FROM providers WHERE id = ?", (provider_id,))
        return rows[0]["name"] if rows else ""

    def reload_service_options(self):
        services = query_all("SELECT id, type || ' - ' || name AS name FROM services WHERE active = 1 ORDER BY type, name")
        self.service_options = {row["name"]: row["id"] for row in services}
        if hasattr(self, "service_combo"):
            self.service_combo["values"] = list(self.service_options.keys())

    def service_name_by_id(self, service_id):
        if not service_id:
            return ""
        rows = query_all("SELECT type || ' - ' || name AS name FROM services WHERE id = ?", (service_id,))
        return rows[0]["name"] if rows else ""

    def reload_client_aula_options(self):
        if self.table != "clients":
            return
        rows = query_all(
            """
            SELECT s.id, s.name, COALESCE(s.price, '') AS price,
                   COALESCE(
                       direct_provider.name,
                       (
                           SELECT GROUP_CONCAT(assigned_provider.name, ', ')
                           FROM providers assigned_provider
                           WHERE assigned_provider.active = 1
                             AND assigned_provider.selected_service_id = s.id
                       ),
                       ''
                   ) AS provider_name
            FROM services s
            LEFT JOIN providers direct_provider ON direct_provider.id = s.provider_id
            WHERE s.active = 1 AND s.type = 'Aula'
            ORDER BY s.name
            """
        )
        self.client_aula_options = {
            row["name"]: {
                "id": row["id"],
                "provider_name": row["provider_name"],
                "price": row["price"],
            }
            for row in rows
        }
        self.client_service_combo["values"] = list(self.client_aula_options.keys())

    def on_client_service_selected(self, _event=None):
        aula = self.client_aula_options.get(self.client_service_var.get().strip())
        if not aula:
            self.client_provider_var.set("")
            self.client_price_var.set("")
            return
        self.client_provider_var.set(aula["provider_name"])
        self.client_price_var.set("" if aula["price"] == "" else f"{aula['price']:.2f}")

    def load_client_service_fields(self, row):
        self.reload_client_aula_options()
        selected_service_id = row["selected_service_id"]
        selected_name = ""
        for name, aula in self.client_aula_options.items():
            if aula["id"] == selected_service_id:
                selected_name = name
                break
        self.client_service_var.set(selected_name)
        self.client_start_date_var.set(row["service_start_date"] or "")
        self.client_end_date_var.set(row["service_end_date"] or "")
        self.on_client_service_selected()


class AgendaTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)
        today = date.today()
        self.year_var = StringVar(value=str(today.year))
        self.week_start = week_start_for(today)
        self.provider_var = StringVar()
        self.day_start_var = StringVar(value="08:00")
        self.day_end_var = StringVar(value="20:00")
        self.slot_minutes_var = StringVar(value="30")
        self.window_size_var = StringVar(value="Maximizar")
        self.selected_id = None
        self.selected_slot = None
        self.drag_data = None
        self.combo_maps = {"providers": {}, "services": {}}
        self.week_rows = []
        self.item_to_appointment = {}
        self.appointment_notes = {}
        self.tooltip_window = None
        self.tooltip_appointment_id = None
        self.schedule_start_minutes = 8 * 60
        self.schedule_end_minutes = 20 * 60
        self.slot_minutes = 30
        self.time_col_width = 70
        self.header_height = 38
        self.slot_height = 34
        self.day_width = 150
        self.vars = {
            "date": StringVar(value=today.isoformat()),
            "start": StringVar(value="09:00"),
            "end": StringVar(value="10:00"),
            "provider": StringVar(),
            "service": StringVar(),
            "state": StringVar(value=APPOINTMENT_STATES[0]),
            "notes": StringVar(),
        }
        self.build()
        self.reload_options()
        self.load_week()

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew")
        ttk.Label(toolbar, text="Ano").pack(side="left")
        ttk.Spinbox(toolbar, from_=2000, to=2100, textvariable=self.year_var, width=8, command=self.change_year).pack(
            side="left", padx=(6, 16)
        )
        ttk.Button(toolbar, text="< Semana", command=lambda: self.move_week(-1)).pack(side="left")
        ttk.Button(toolbar, text="Hoje", command=self.go_today).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Semana >", command=lambda: self.move_week(1)).pack(side="left")
        ttk.Label(toolbar, text="Prestador").pack(side="left", padx=(22, 6))
        self.provider_filter = ttk.Combobox(toolbar, textvariable=self.provider_var, state="readonly", width=28)
        self.provider_filter.pack(side="left")
        self.provider_filter.bind("<<ComboboxSelected>>", lambda _event: self.load_week())
        ttk.Label(toolbar, text="Das").pack(side="left", padx=(18, 6))
        ttk.Entry(toolbar, textvariable=self.day_start_var, width=6).pack(side="left")
        ttk.Label(toolbar, text="as").pack(side="left", padx=6)
        ttk.Entry(toolbar, textvariable=self.day_end_var, width=6).pack(side="left")
        ttk.Label(toolbar, text="Bloco").pack(side="left", padx=(18, 6))
        self.slot_combo = ttk.Combobox(
            toolbar,
            textvariable=self.slot_minutes_var,
            values=("15", "30", "60"),
            state="readonly",
            width=5,
        )
        self.slot_combo.pack(side="left")
        self.slot_combo.bind("<<ComboboxSelected>>", lambda _event: self.load_week())
        ttk.Button(toolbar, text="Atualizar", command=self.load_week).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, textvariable=self.window_size_var, command=self.toggle_window_size).pack(
            side="left",
            padx=(8, 0),
        )

        self.week_label = ttk.Label(self, text="", font=("Segoe UI Semibold", 11))
        self.week_label.grid(row=1, column=0, sticky="w", pady=(12, 6))

        grid_frame = ttk.Frame(self)
        grid_frame.grid(row=2, column=0, sticky="nsew")
        grid_frame.columnconfigure(0, weight=1)
        grid_frame.rowconfigure(0, weight=1)
        self.canvas = Canvas(grid_frame, bg=SURFACE_COLOR, highlightthickness=1, highlightbackground=BORDER_COLOR)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(grid_frame, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Double-Button-1>", self.on_canvas_double_click)
        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Leave>", self.hide_notes_tooltip)
        self.canvas.bind("<Configure>", lambda _event: self.draw_schedule())

        form = ttk.LabelFrame(self, text="Marcacao", padding=12)
        form.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        for col in range(8):
            form.columnconfigure(col, weight=1 if col % 2 else 0)

        fields = [
            ("Data", "date", 0, 0),
            ("Inicio", "start", 0, 2),
            ("Fim", "end", 0, 4),
            ("Estado", "state", 0, 6),
            ("Prestador", "provider", 1, 0),
            ("Servico", "service", 1, 2),
            ("Notas", "notes", 2, 0),
        ]
        for label, key, row, col in fields:
            ttk.Label(form, text=label).grid(row=row, column=col, sticky="w", pady=4)
            if key == "state":
                widget = ttk.Combobox(form, textvariable=self.vars[key], values=APPOINTMENT_STATES, state="readonly")
            elif key in {"provider", "service"}:
                widget = ttk.Combobox(form, textvariable=self.vars[key], state="readonly", width=24)
                setattr(self, f"{key}_combo", widget)
                if key == "provider":
                    widget.bind("<<ComboboxSelected>>", self.on_appointment_provider_selected)
            else:
                widget = ttk.Entry(form, textvariable=self.vars[key])
            span = 7 if key == "notes" else 1
            widget.grid(row=row, column=col + 1, columnspan=span, sticky="ew", padx=(6, 14), pady=4)

        actions = ttk.Frame(form)
        actions.grid(row=3, column=0, columnspan=8, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="Nova", command=self.clear_form).pack(side="left")
        ttk.Button(actions, text="Criar no bloco selecionado", command=self.new_from_selected_slot).pack(
            side="left",
            padx=(8, 0),
        )
        ttk.Button(actions, text="Guardar", command=self.save).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Cancelar marcacao", command=self.cancel_selected).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Remover marcacao", command=self.delete_selected, style="Danger.TButton").pack(
            side="left",
            padx=(8, 0),
        )
        self.selection_label = ttk.Label(form, text="Clique num bloco livre para preparar uma nova marcacao.")
        self.selection_label.grid(row=4, column=0, columnspan=8, sticky="w", pady=(8, 0))

    def reload_options(self):
        providers = query_all("SELECT id, name FROM providers WHERE active = 1 ORDER BY name")
        services = query_all("SELECT id, type || ' - ' || name AS name FROM services WHERE active = 1 ORDER BY type, name")

        self.combo_maps["providers"] = {row["name"]: row["id"] for row in providers}
        self.combo_maps["services"] = {row["name"]: row["id"] for row in services}

        provider_names = list(self.combo_maps["providers"].keys())
        service_names = list(self.combo_maps["services"].keys())

        self.provider_filter["values"] = ["Todos"] + provider_names
        if not self.provider_var.get():
            self.provider_var.set("Todos")
        self.provider_combo["values"] = provider_names
        self.service_combo["values"] = service_names
        self.refresh_appointment_services()

    def on_appointment_provider_selected(self, _event=None):
        self.refresh_appointment_services()

    def refresh_appointment_services(self):
        provider_id = self.combo_maps["providers"].get(self.vars["provider"].get())
        if not provider_id:
            service_names = list(self.combo_maps["services"].keys())
        else:
            rows = query_all(
                """
                SELECT DISTINCT s.id, s.type || ' - ' || s.name AS name
                FROM services s
                LEFT JOIN providers p ON p.selected_service_id = s.id
                WHERE s.active = 1
                  AND (s.provider_id = ? OR p.id = ?)
                ORDER BY s.type, s.name
                """,
                (provider_id, provider_id),
            )
            service_names = [row["name"] for row in rows]
        self.service_combo["values"] = service_names
        current_service = self.vars["service"].get()
        if current_service and current_service not in service_names:
            self.vars["service"].set("")
        if not self.vars["service"].get() and len(service_names) == 1:
            self.vars["service"].set(service_names[0])

    def selected_provider_id(self):
        provider = self.provider_var.get()
        if provider == "Todos":
            return None
        return self.combo_maps["providers"].get(provider)

    def change_year(self):
        try:
            year = int(self.year_var.get())
        except ValueError:
            year = date.today().year
            self.year_var.set(str(year))
        self.week_start = clamp_week_to_year(self.week_start, year)
        self.load_week()

    def move_week(self, amount):
        self.week_start += timedelta(days=amount * 7)
        self.year_var.set(str(self.week_start.year))
        self.load_week()

    def go_today(self):
        today = date.today()
        self.year_var.set(str(today.year))
        self.week_start = week_start_for(today)
        self.load_week()

    def toggle_window_size(self):
        window = self.winfo_toplevel()
        try:
            if window.state() == "zoomed":
                window.state("normal")
                window.geometry("1400x900")
                self.window_size_var.set("Maximizar")
            else:
                window.state("zoomed")
                self.window_size_var.set("Restaurar")
        except Exception:
            if self.window_size_var.get() == "Maximizar":
                window.geometry("1500x940")
                self.window_size_var.set("Restaurar")
            else:
                window.geometry("1400x900")
                self.window_size_var.set("Maximizar")

    def clear_form(self):
        self.selected_id = None
        self.selected_slot = None
        self.vars["date"].set(date.today().isoformat())
        self.vars["start"].set("09:00")
        self.vars["end"].set("10:00")
        self.vars["state"].set(APPOINTMENT_STATES[0])
        self.vars["notes"].set("")
        for key in ("provider", "service"):
            self.vars[key].set("")
        self.refresh_appointment_services()
        self.selection_label.configure(text="Clique num bloco livre para preparar uma nova marcacao.")
        self.draw_schedule()

    def new_from_selected_slot(self):
        if not self.selected_slot:
            messagebox.showinfo("Sem bloco", "Clique primeiro num bloco livre da grelha.")
            return
        selected_date, start_minutes, end_minutes = self.selected_slot
        self.selected_id = None
        self.vars["date"].set(selected_date.isoformat())
        self.vars["start"].set(format_minutes(start_minutes))
        self.vars["end"].set(format_minutes(end_minutes))
        self.vars["state"].set(APPOINTMENT_STATES[0])
        self.vars["notes"].set("")
        provider = self.provider_var.get()
        if provider != "Todos":
            self.vars["provider"].set(provider)
            self.refresh_appointment_services()

    def save(self):
        try:
            selected_date = parse_date(self.vars["date"].get().strip())
            validate_time_range(self.vars["start"].get().strip(), self.vars["end"].get().strip())
        except ValueError as exc:
            messagebox.showwarning("Dados invalidos", str(exc))
            return

        provider_id = self.combo_maps["providers"].get(self.vars["provider"].get())
        service_id = self.combo_maps["services"].get(self.vars["service"].get())
        if not provider_id or not service_id:
            messagebox.showwarning("Dados em falta", "Escolha o prestador e o servico.")
            return

        conflicts = self.find_conflicts(
            selected_date.isoformat(),
            self.vars["start"].get().strip(),
            self.vars["end"].get().strip(),
            provider_id,
        )
        if conflicts:
            if not messagebox.askyesno(
                "Conflito de horario",
                "Ja existe uma marcacao sobreposta para este prestador. Pretende guardar na mesma?",
            ):
                return

        values = (
            selected_date.isoformat(),
            self.vars["start"].get().strip(),
            self.vars["end"].get().strip(),
            ensure_default_client_id(),
            provider_id,
            service_id,
            self.vars["state"].get(),
            self.vars["notes"].get().strip(),
        )
        if self.selected_id:
            appointment_id = self.selected_id
            execute(
                """
                UPDATE appointments
                SET appointment_date = ?, start_time = ?, end_time = ?, client_id = ?,
                    provider_id = ?, service_id = ?, state = ?, notes = ?
                WHERE id = ?
                """,
                values + (self.selected_id,),
            )
        else:
            appointment_id = execute(
                """
                INSERT INTO appointments (
                    appointment_date, start_time, end_time, client_id,
                    provider_id, service_id, state, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        replace_appointment_clients(appointment_id, [])
        self.week_start = week_start_for(selected_date)
        self.year_var.set(str(selected_date.year))
        self.load_week()
        self.clear_form()

    def find_conflicts(self, selected_date, start_time, end_time, provider_id, ignore_id=None):
        params = [selected_date, provider_id, end_time, start_time]
        ignore_clause = ""
        ignored_appointment_id = ignore_id if ignore_id is not None else self.selected_id
        if ignored_appointment_id:
            ignore_clause = "AND id <> ?"
            params.append(ignored_appointment_id)
        return query_all(
            f"""
            SELECT id FROM appointments
            WHERE appointment_date = ?
              AND provider_id = ?
              AND state <> 'Cancelado'
              AND start_time < ?
              AND end_time > ?
              {ignore_clause}
            """,
            params,
        )

    def cancel_selected(self):
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha uma marcacao para cancelar.")
            return
        execute("UPDATE appointments SET state = 'Cancelado' WHERE id = ?", (self.selected_id,))
        self.load_week()
        self.clear_form()

    def delete_selected(self):
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha uma marcacao para remover.")
            return
        if not messagebox.askyesno(
            "Confirmar remocao",
            "Tem a certeza que pretende remover definitivamente esta marcacao?",
        ):
            return
        execute("DELETE FROM appointments WHERE id = ?", (self.selected_id,))
        self.load_week()
        self.clear_form()

    def parse_schedule_settings(self):
        start_minutes = minutes_from_time(self.day_start_var.get().strip())
        end_minutes = minutes_from_time(self.day_end_var.get().strip())
        slot_minutes = int(self.slot_minutes_var.get())
        if slot_minutes not in {15, 30, 60}:
            raise ValueError("Escolha um bloco de 15, 30 ou 60 minutos.")
        if start_minutes >= end_minutes:
            raise ValueError("A hora final da grelha deve ser posterior a hora inicial.")
        if (end_minutes - start_minutes) < slot_minutes:
            raise ValueError("O intervalo da grelha deve ter pelo menos um bloco.")
        return start_minutes, end_minutes, slot_minutes

    def load_week(self):
        try:
            year = int(self.year_var.get())
        except ValueError:
            year = date.today().year
            self.year_var.set(str(year))
        try:
            self.schedule_start_minutes, self.schedule_end_minutes, self.slot_minutes = self.parse_schedule_settings()
        except ValueError as exc:
            messagebox.showwarning("Horario invalido", str(exc))
            return

        start = self.week_start
        end = start + timedelta(days=6)
        provider_title = self.provider_var.get() or "Todos"
        self.week_label.configure(text=f"Prestador: {provider_title}    |    Semana de {start:%d/%m/%Y} a {end:%d/%m/%Y}")

        provider_id = self.selected_provider_id()
        sql = """
            SELECT a.*, p.name AS provider_name,
                   s.type || ' - ' || s.name AS service_name
            FROM appointments a
            JOIN providers p ON p.id = a.provider_id
            JOIN services s ON s.id = a.service_id
            WHERE a.appointment_date BETWEEN ? AND ?
              AND substr(a.appointment_date, 1, 4) = ?
        """
        params = [start.isoformat(), end.isoformat(), str(year)]
        if provider_id:
            sql += " AND a.provider_id = ?"
            params.append(provider_id)
        sql += " ORDER BY a.appointment_date, a.start_time"
        self.week_rows = query_all(sql, params)
        self.draw_schedule()

    def draw_schedule(self):
        if not hasattr(self, "canvas"):
            return

        self.hide_notes_tooltip()
        self.canvas.delete("all")
        self.item_to_appointment = {}
        self.appointment_notes = {}
        canvas_width = max(self.canvas.winfo_width(), 900)
        slot_count = (self.schedule_end_minutes - self.schedule_start_minutes) // self.slot_minutes
        self.day_width = max(118, (canvas_width - self.time_col_width - 6) // 7)
        total_width = self.time_col_width + self.day_width * 7
        total_height = self.header_height + slot_count * self.slot_height + 8
        self.canvas.configure(scrollregion=(0, 0, total_width, total_height))

        self.canvas.create_rectangle(0, 0, total_width, self.header_height, fill=SURFACE_ALT_COLOR, outline=BORDER_COLOR)
        for day_index, day_name in enumerate(WEEK_DAYS):
            day = self.week_start + timedelta(days=day_index)
            x1 = self.time_col_width + day_index * self.day_width
            x2 = x1 + self.day_width
            self.canvas.create_rectangle(x1, 0, x2, self.header_height, fill=SURFACE_ALT_COLOR, outline=BORDER_COLOR)
            self.canvas.create_text(
                (x1 + x2) / 2,
                19,
                text=f"{day_name}\n{day:%d/%m}",
                fill=TEXT_COLOR,
                font=("Segoe UI Semibold", 9),
                justify="center",
            )

        for slot_index in range(slot_count + 1):
            minutes = self.schedule_start_minutes + slot_index * self.slot_minutes
            y = self.header_height + slot_index * self.slot_height
            line_color = BORDER_COLOR if minutes % 60 == 0 else "#e5e7eb"
            self.canvas.create_line(self.time_col_width, y, total_width, y, fill=line_color)
            if slot_index < slot_count:
                self.canvas.create_text(
                    self.time_col_width - 8,
                    y + 5,
                    text=format_minutes(minutes),
                    anchor="ne",
                    fill=MUTED_COLOR,
                    font=("Segoe UI", 8),
                )

        for day_index in range(8):
            x = self.time_col_width + day_index * self.day_width
            self.canvas.create_line(x, 0, x, total_height, fill=BORDER_COLOR)

        if self.selected_slot:
            selected_date, start_minutes, end_minutes = self.selected_slot
            day_index = (selected_date - self.week_start).days
            if 0 <= day_index <= 6:
                self.draw_slot_highlight(day_index, start_minutes, end_minutes)

        for row in self.week_rows:
            self.draw_appointment(row)

    def draw_slot_highlight(self, day_index, start_minutes, end_minutes):
        start_minutes = max(start_minutes, self.schedule_start_minutes)
        end_minutes = min(end_minutes, self.schedule_end_minutes)
        if start_minutes >= end_minutes:
            return
        x1 = self.time_col_width + day_index * self.day_width + 2
        x2 = x1 + self.day_width - 4
        y1 = self.header_height + ((start_minutes - self.schedule_start_minutes) / self.slot_minutes) * self.slot_height
        y2 = self.header_height + ((end_minutes - self.schedule_start_minutes) / self.slot_minutes) * self.slot_height
        self.canvas.create_rectangle(x1, y1, x2, y2, fill="#dbeafe", outline="#2563eb", dash=(3, 2))

    def draw_appointment(self, row):
        day_index = parse_date(row["appointment_date"]).weekday()
        start_minutes = minutes_from_time(row["start_time"])
        end_minutes = minutes_from_time(row["end_time"])
        if end_minutes <= self.schedule_start_minutes or start_minutes >= self.schedule_end_minutes:
            return

        visible_start = max(start_minutes, self.schedule_start_minutes)
        visible_end = min(end_minutes, self.schedule_end_minutes)
        x1 = self.time_col_width + day_index * self.day_width + 5
        x2 = x1 + self.day_width - 10
        y1 = self.header_height + ((visible_start - self.schedule_start_minutes) / self.slot_minutes) * self.slot_height + 3
        y2 = self.header_height + ((visible_end - self.schedule_start_minutes) / self.slot_minutes) * self.slot_height - 3
        if y2 - y1 < 58:
            y2 = y1 + 58

        fill, outline, text_color = self.colors_for_state(row["state"])
        if row["id"] == self.selected_id:
            outline = "#111827"
        rect = self.canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline=outline, width=2)
        provider_bar = self.canvas.create_rectangle(x1, y1, x2, min(y1 + 23, y2), fill=outline, outline=outline)
        provider_text = self.canvas.create_text(
            x1 + 6,
            y1 + 4,
            text=row["provider_name"],
            anchor="nw",
            width=max(40, self.day_width - 22),
            fill="#ffffff",
            font=("Segoe UI Semibold", 8),
        )
        text = f"{row['start_time']}-{row['end_time']}\n{row['service_name']}\n{row['state']}"
        text_item = self.canvas.create_text(
            x1 + 6,
            y1 + 28,
            text=text,
            anchor="nw",
            width=max(40, self.day_width - 22),
            fill=text_color,
            font=("Segoe UI", 8),
        )
        self.item_to_appointment[rect] = row["id"]
        self.item_to_appointment[provider_bar] = row["id"]
        self.item_to_appointment[provider_text] = row["id"]
        self.item_to_appointment[text_item] = row["id"]
        self.appointment_notes[row["id"]] = row["notes"].strip()

    def colors_for_state(self, state):
        if state == "Concluido":
            return "#dcfce7", "#15803d", "#14532d"
        if state == "Cancelado":
            return "#ffe4e6", "#be123c", "#7f1d1d"
        return "#ccfbf1", PRIMARY_COLOR, "#134e4a"

    def appointment_from_event(self, event):
        current = self.canvas.find_withtag("current")
        if not current:
            return None
        return self.item_to_appointment.get(current[0])

    def on_canvas_motion(self, event):
        appointment_id = self.appointment_from_event(event)
        if not appointment_id:
            self.hide_notes_tooltip()
            return

        notes = self.appointment_notes.get(appointment_id, "")
        if not notes:
            self.hide_notes_tooltip()
            return

        if self.tooltip_appointment_id != appointment_id:
            self.show_notes_tooltip(event, appointment_id, notes)
        else:
            self.move_notes_tooltip(event)

    def show_notes_tooltip(self, event, appointment_id, notes):
        self.hide_notes_tooltip()
        tooltip = Toplevel(self)
        tooltip.wm_overrideredirect(True)
        tooltip.configure(bg="#111827")
        label = ttk.Label(
            tooltip,
            text=f"Notas:\n{notes}",
            background="#111827",
            foreground="#ffffff",
            padding=(10, 8),
            justify="left",
            wraplength=320,
        )
        label.pack()
        self.tooltip_window = tooltip
        self.tooltip_appointment_id = appointment_id
        self.move_notes_tooltip(event)

    def move_notes_tooltip(self, event):
        if not self.tooltip_window:
            return
        x = self.canvas.winfo_rootx() + event.x + 16
        y = self.canvas.winfo_rooty() + event.y + 16
        self.tooltip_window.geometry(f"+{x}+{y}")

    def hide_notes_tooltip(self, _event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None
        self.tooltip_appointment_id = None

    def slot_from_event(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        if x < self.time_col_width or y < self.header_height:
            return None

        day_index = int((x - self.time_col_width) // self.day_width)
        if day_index < 0 or day_index > 6:
            return None
        slot_index = int((y - self.header_height) // self.slot_height)
        start_minutes = self.schedule_start_minutes + slot_index * self.slot_minutes
        if start_minutes < self.schedule_start_minutes or start_minutes >= self.schedule_end_minutes:
            return None

        selected_date = self.week_start + timedelta(days=day_index)
        return selected_date, start_minutes, min(start_minutes + self.slot_minutes, self.schedule_end_minutes)

    def on_canvas_press(self, event):
        self.hide_notes_tooltip()
        appointment_id = self.appointment_from_event(event)
        self.drag_data = {
            "appointment_id": appointment_id,
            "start_x": self.canvas.canvasx(event.x),
            "start_y": self.canvas.canvasy(event.y),
            "moved": False,
            "target": None,
        }

    def on_canvas_drag(self, event):
        if not self.drag_data or not self.drag_data["appointment_id"]:
            return

        current_x = self.canvas.canvasx(event.x)
        current_y = self.canvas.canvasy(event.y)
        moved_x = abs(current_x - self.drag_data["start_x"])
        moved_y = abs(current_y - self.drag_data["start_y"])
        if moved_x < 5 and moved_y < 5:
            return

        slot = self.slot_from_event(event)
        if not slot:
            return
        appointment = self.get_appointment_row(self.drag_data["appointment_id"])
        if not appointment:
            return

        selected_date, start_minutes, _end_minutes = slot
        duration = minutes_from_time(appointment["end_time"]) - minutes_from_time(appointment["start_time"])
        end_minutes = start_minutes + duration
        if end_minutes > self.schedule_end_minutes:
            start_minutes = max(self.schedule_start_minutes, self.schedule_end_minutes - duration)
            end_minutes = start_minutes + duration

        self.drag_data["moved"] = True
        self.drag_data["target"] = (selected_date, start_minutes, end_minutes)
        self.selected_id = int(appointment["id"])
        self.selected_slot = (selected_date, start_minutes, min(end_minutes, self.schedule_end_minutes))
        self.selection_label.configure(
            text=f"Mover para {selected_date:%d/%m/%Y}, das {format_minutes(start_minutes)} as {format_minutes(end_minutes)}."
        )
        self.draw_schedule()

    def on_canvas_release(self, event):
        if not self.drag_data:
            return

        appointment_id = self.drag_data["appointment_id"]
        moved = self.drag_data["moved"]
        target = self.drag_data["target"]
        self.drag_data = None

        if appointment_id and moved and target:
            self.move_appointment(appointment_id, *target)
            return

        if appointment_id:
            self.load_appointment(appointment_id)
            return

        self.select_slot_from_event(event)

    def select_slot_from_event(self, event):
        slot = self.slot_from_event(event)
        if not slot:
            return
        selected_date, start_minutes, end_minutes = slot
        self.selected_id = None
        self.selected_slot = (selected_date, start_minutes, end_minutes)
        self.vars["date"].set(selected_date.isoformat())
        self.vars["start"].set(format_minutes(start_minutes))
        self.vars["end"].set(format_minutes(end_minutes))
        self.vars["state"].set(APPOINTMENT_STATES[0])
        self.selection_label.configure(
            text=f"Bloco selecionado: {selected_date:%d/%m/%Y} das {format_minutes(start_minutes)} as {format_minutes(end_minutes)}."
        )
        provider = self.provider_var.get()
        if provider != "Todos":
            self.vars["provider"].set(provider)
        self.draw_schedule()

    def get_appointment_row(self, appointment_id):
        rows = query_all("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        return rows[0] if rows else None

    def move_appointment(self, appointment_id, selected_date, start_minutes, end_minutes):
        appointment = self.get_appointment_row(appointment_id)
        if not appointment:
            return

        start_time = format_minutes(start_minutes)
        end_time = format_minutes(end_minutes)
        conflicts = self.find_conflicts(
            selected_date.isoformat(),
            start_time,
            end_time,
            appointment["provider_id"],
            ignore_id=appointment_id,
        )
        if conflicts:
            if not messagebox.askyesno(
                "Conflito de horario",
                "Ja existe uma marcacao sobreposta para este prestador no novo horario. Pretende mover na mesma?",
            ):
                self.selected_slot = None
                self.draw_schedule()
                return

        execute(
            """
            UPDATE appointments
            SET appointment_date = ?, start_time = ?, end_time = ?
            WHERE id = ?
            """,
            (selected_date.isoformat(), start_time, end_time, appointment_id),
        )
        self.week_start = week_start_for(selected_date)
        self.year_var.set(str(selected_date.year))
        self.load_week()
        self.load_appointment(appointment_id)
        self.selection_label.configure(
            text=f"Marcacao movida para {selected_date:%d/%m/%Y}, das {start_time} as {end_time}."
        )

    def on_canvas_double_click(self, event):
        appointment_id = self.appointment_from_event(event)
        if appointment_id:
            self.load_appointment(appointment_id)
            return
        self.new_from_selected_slot()

    def load_appointment(self, appointment_id):
        self.selected_id = int(appointment_id)
        self.selected_slot = None
        row = query_all(
            """
            SELECT a.*, p.name AS provider_name,
                   s.type || ' - ' || s.name AS service_name
            FROM appointments a
            JOIN providers p ON p.id = a.provider_id
            JOIN services s ON s.id = a.service_id
            WHERE a.id = ?
            """,
            (self.selected_id,),
        )[0]
        self.vars["date"].set(row["appointment_date"])
        self.vars["start"].set(row["start_time"])
        self.vars["end"].set(row["end_time"])
        self.vars["provider"].set(row["provider_name"])
        self.refresh_appointment_services()
        self.vars["service"].set(row["service_name"])
        self.vars["state"].set(row["state"])
        self.vars["notes"].set(row["notes"])
        self.selection_label.configure(text="Marcacao selecionada. Altere os campos e carregue em Guardar.")
        self.draw_schedule()


class BackupTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=24)
        self.status_var = StringVar(value=f"Base de dados: {DB_FILE}")
        ttk.Label(self, text="Backup da base de dados", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            self,
            text="Cria uma copia completa do ficheiro SQLite para o local escolhido.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 18))
        ttk.Button(self, text="Criar backup", command=self.backup, style="Primary.TButton").pack(anchor="w")
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(18, 0))

    def backup(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = filedialog.asksaveasfilename(
            title="Guardar backup",
            defaultextension=".db",
            initialfile=f"agenda_backup_{timestamp}.db",
            filetypes=[("Base SQLite", "*.db"), ("Todos os ficheiros", "*.*")],
        )
        if not destination:
            return
        shutil.copy2(DB_FILE, destination)
        self.status_var.set(f"Backup criado: {destination}")
        messagebox.showinfo("Backup concluido", "Backup criado com sucesso.")


class PaymentsTab(ttk.Frame):
    def __init__(self, parent, reload_callback=None):
        super().__init__(parent, padding=16)
        self.reload_callback = reload_callback
        self.selected_id = None
        self.client_options = {}
        self.service_options = {}
        self.client_service_ids = {}
        self.search_var = StringVar()
        self.vars = {
            "client": StringVar(),
            "service": StringVar(),
            "period_start_date": StringVar(),
            "period_end_date": StringVar(),
            "amount": StringVar(),
            "state": StringVar(value=PAYMENT_STATES[0]),
            "payment_date": StringVar(),
            "notes": StringVar(),
        }
        self.build()
        self.reload_options()
        self.load_rows()

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(self, text="Pagamentos", padding=12)
        form.grid(row=0, column=0, sticky="ew")
        for column in (1, 3):
            form.columnconfigure(column, weight=1)

        ttk.Label(form, text="Cliente").grid(row=0, column=0, sticky="w", pady=4)
        self.client_combo = ttk.Combobox(form, textvariable=self.vars["client"], state="readonly")
        self.client_combo.grid(row=0, column=1, sticky="ew", padx=(8, 16), pady=4)
        self.client_combo.bind("<<ComboboxSelected>>", self.on_client_selected)

        ttk.Label(form, text="Aula/Servico").grid(row=0, column=2, sticky="w", pady=4)
        self.service_combo = ttk.Combobox(form, textvariable=self.vars["service"], state="readonly")
        self.service_combo.grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=4)
        self.service_combo.bind("<<ComboboxSelected>>", self.on_service_selected)

        ttk.Label(form, text="Data inicio").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(form, textvariable=self.vars["period_start_date"], values=date_options(), width=12).grid(
            row=1,
            column=1,
            sticky="w",
            padx=(8, 16),
            pady=4,
        )

        ttk.Label(form, text="Data fim").grid(row=1, column=2, sticky="w", pady=4)
        ttk.Combobox(form, textvariable=self.vars["period_end_date"], values=date_options(), width=12).grid(
            row=1,
            column=3,
            sticky="w",
            padx=(8, 0),
            pady=4,
        )

        ttk.Label(form, text="Valor").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.vars["amount"], width=14).grid(row=2, column=1, sticky="w", padx=(8, 16), pady=4)

        ttk.Label(form, text="Estado").grid(row=2, column=2, sticky="w", pady=4)
        ttk.Combobox(form, textvariable=self.vars["state"], values=PAYMENT_STATES, state="readonly", width=14).grid(
            row=2,
            column=3,
            sticky="w",
            padx=(8, 0),
            pady=4,
        )

        ttk.Label(form, text="Data pagamento").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(form, textvariable=self.vars["payment_date"], values=date_options(), width=12).grid(
            row=3,
            column=1,
            sticky="w",
            padx=(8, 16),
            pady=4,
        )

        ttk.Label(form, text="Notas").grid(row=3, column=2, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.vars["notes"]).grid(row=3, column=3, sticky="ew", padx=(8, 0), pady=4)

        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        ttk.Button(actions, text="Novo", command=self.clear_form).pack(side="left")
        ttk.Button(actions, text="Guardar", command=self.save, style="Primary.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Marcar pago", command=self.mark_paid).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Cancelar", command=self.cancel_payment).pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="Pesquisar cliente").pack(side="left", padx=(24, 6))
        search = ttk.Entry(actions, textvariable=self.search_var, width=30)
        search.pack(side="left")
        search.bind("<KeyRelease>", lambda _event: self.load_rows())

        columns = ("client", "service", "period", "amount", "state", "payment_date", "notes")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=15)
        headings = {
            "client": "Cliente",
            "service": "Aula/Servico",
            "period": "Periodo",
            "amount": "Valor",
            "state": "Estado",
            "payment_date": "Data pagamento",
            "notes": "Notas",
        }
        widths = {
            "client": 190,
            "service": 190,
            "period": 190,
            "amount": 90,
            "state": 105,
            "payment_date": 120,
            "notes": 260,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=80, stretch=False)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        vertical_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vertical_scrollbar.grid(row=2, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal_scrollbar.grid(row=3, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        self.tree.bind("<Shift-MouseWheel>", self.on_tree_shift_mousewheel)

    def reload_options(self):
        clients = query_all(
            """
            SELECT id, name, selected_service_id
            FROM clients
            WHERE active = 1
            ORDER BY name
            """
        )
        self.client_options = {row["name"]: row["id"] for row in clients}
        self.client_service_ids = {row["name"]: row["selected_service_id"] for row in clients}
        self.client_combo["values"] = list(self.client_options.keys())

        services = query_all(
            """
            SELECT id, type || ' - ' || name AS name, COALESCE(price, 0) AS price
            FROM services
            WHERE active = 1
            ORDER BY type, name
            """
        )
        self.service_options = {
            row["name"]: {
                "id": row["id"],
                "price": float(row["price"] or 0),
            }
            for row in services
        }
        self.service_combo["values"] = list(self.service_options.keys())

    def on_client_selected(self, _event=None):
        service_id = self.client_service_ids.get(self.vars["client"].get().strip())
        if not service_id:
            return
        for name, service in self.service_options.items():
            if service["id"] == service_id:
                self.vars["service"].set(name)
                self.on_service_selected()
                break

    def on_service_selected(self, _event=None):
        service = self.service_options.get(self.vars["service"].get().strip())
        if service:
            self.vars["amount"].set(format_currency(service["price"]))

    def row_values(self):
        service = self.service_options.get(self.vars["service"].get().strip())
        return {
            "client_id": self.client_options.get(self.vars["client"].get().strip()),
            "service_id": service["id"] if service else None,
            "period_start_date": self.vars["period_start_date"].get().strip(),
            "period_end_date": self.vars["period_end_date"].get().strip(),
            "amount": self.vars["amount"].get().strip(),
            "state": self.vars["state"].get().strip(),
            "payment_date": self.vars["payment_date"].get().strip(),
            "notes": self.vars["notes"].get().strip(),
        }

    def validate_values(self, values):
        if not values["client_id"]:
            messagebox.showwarning("Campo obrigatorio", "Escolha o cliente.")
            return None
        if not values["service_id"]:
            messagebox.showwarning("Campo obrigatorio", "Escolha a aula/servico.")
            return None
        if values["state"] not in PAYMENT_STATES:
            messagebox.showwarning("Estado invalido", "Escolha um estado valido.")
            return None
        for key, label in (
            ("period_start_date", "Data inicio"),
            ("period_end_date", "Data fim"),
            ("payment_date", "Data pagamento"),
        ):
            if values[key]:
                try:
                    parse_date(values[key])
                except ValueError:
                    messagebox.showwarning("Data invalida", f"Indique uma data valida em {label}: AAAA-MM-DD.")
                    return None
        if values["period_start_date"] and values["period_end_date"]:
            if parse_date(values["period_end_date"]) < parse_date(values["period_start_date"]):
                messagebox.showwarning("Periodo invalido", "A data fim deve ser igual ou posterior a data inicio.")
                return None
        try:
            values["amount"] = float(values["amount"].replace(",", ".") or 0)
        except ValueError:
            messagebox.showwarning("Valor invalido", "Indique um valor numerico.")
            return None
        if values["state"] == "Pago" and not values["payment_date"]:
            values["payment_date"] = date.today().isoformat()
            self.vars["payment_date"].set(values["payment_date"])
        return values

    def save(self):
        values = self.validate_values(self.row_values())
        if values is None:
            return
        keys = list(values.keys())
        if self.selected_id:
            assignments = ", ".join(f"{key} = ?" for key in keys)
            execute(
                f"UPDATE payments SET {assignments} WHERE id = ?",
                [values[key] for key in keys] + [self.selected_id],
            )
        else:
            placeholders = ", ".join("?" for _ in keys)
            execute(
                f"INSERT INTO payments ({', '.join(keys)}) VALUES ({placeholders})",
                [values[key] for key in keys],
            )
        self.load_rows()
        self.clear_form()
        if self.reload_callback:
            self.reload_callback()

    def mark_paid(self):
        self.vars["state"].set("Pago")
        self.vars["payment_date"].set(date.today().isoformat())
        self.save()

    def cancel_payment(self):
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha um pagamento para cancelar.")
            return
        execute("UPDATE payments SET state = ? WHERE id = ?", ("Cancelado", self.selected_id))
        self.load_rows()
        self.clear_form()
        if self.reload_callback:
            self.reload_callback()

    def clear_form(self):
        self.selected_id = None
        self.vars["client"].set("")
        self.vars["service"].set("")
        self.vars["period_start_date"].set("")
        self.vars["period_end_date"].set("")
        self.vars["amount"].set("")
        self.vars["state"].set(PAYMENT_STATES[0])
        self.vars["payment_date"].set("")
        self.vars["notes"].set("")
        self.tree.selection_remove(self.tree.selection())

    def load_rows(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        term = f"%{self.search_var.get().strip()}%"
        rows = query_all(
            """
            SELECT pay.id, c.name AS client_name,
                   COALESCE(s.type || ' - ' || s.name, '') AS service_name,
                   pay.period_start_date, pay.period_end_date, pay.amount,
                   pay.state, pay.payment_date, pay.notes
            FROM payments pay
            JOIN clients c ON c.id = pay.client_id
            JOIN services s ON s.id = pay.service_id
            WHERE c.name LIKE ?
            ORDER BY pay.period_start_date DESC, c.name
            """,
            (term,),
        )
        for row in rows:
            period = " a ".join(value for value in (row["period_start_date"], row["period_end_date"]) if value)
            self.tree.insert(
                "",
                END,
                iid=str(row["id"]),
                values=(
                    row["client_name"],
                    row["service_name"],
                    period,
                    format_currency(float(row["amount"] or 0)),
                    row["state"],
                    row["payment_date"],
                    row["notes"],
                ),
            )

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        row = query_all(
            """
            SELECT pay.*, c.name AS client_name, s.type || ' - ' || s.name AS service_name
            FROM payments pay
            JOIN clients c ON c.id = pay.client_id
            JOIN services s ON s.id = pay.service_id
            WHERE pay.id = ?
            """,
            (self.selected_id,),
        )[0]
        self.vars["client"].set(row["client_name"])
        self.vars["service"].set(row["service_name"])
        self.vars["period_start_date"].set(row["period_start_date"])
        self.vars["period_end_date"].set(row["period_end_date"])
        self.vars["amount"].set(format_currency(float(row["amount"] or 0)))
        self.vars["state"].set(row["state"])
        self.vars["payment_date"].set(row["payment_date"])
        self.vars["notes"].set(row["notes"])

    def on_tree_shift_mousewheel(self, event):
        self.tree.xview_scroll(int(-1 * (event.delta / 120)), "units")


class ClientValuesReport(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)
        self.filter_var = StringVar()
        self.build()
        self.load_rows()

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        filters = ttk.Frame(self)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(filters, text="Nome do cliente").pack(side="left")
        entry = ttk.Entry(filters, textvariable=self.filter_var, width=32)
        entry.pack(side="left", padx=(8, 8))
        entry.bind("<KeyRelease>", lambda _event: self.load_rows())
        ttk.Button(filters, text="Atualizar", command=self.load_rows).pack(side="left")

        columns = ("client", "provider", "class", "value", "provider_value", "luz_dourada_value", "payment_state")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=14)
        headings = {
            "client": "Cliente",
            "class": "Aula",
            "value": "Valor aula(s)",
            "provider": "Prestador",
            "provider_value": "Prestador 70%",
            "luz_dourada_value": "Luz Dourada 30%",
            "payment_state": "Estado pagamento",
        }
        widths = {
            "client": 180,
            "class": 170,
            "value": 105,
            "provider": 170,
            "provider_value": 115,
            "luz_dourada_value": 130,
            "payment_state": 135,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=90, stretch=False)
        self.tree.grid(row=1, column=0, sticky="nsew")

        vertical_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vertical_scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal_scrollbar.grid(row=2, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)
        self.tree.bind("<Shift-MouseWheel>", self.on_tree_shift_mousewheel)

    def load_rows(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        term = f"%{self.filter_var.get().strip()}%"
        rows = query_all(
            """
            SELECT c.name AS client_name, COALESCE(s.name, '') AS aula_name,
                   COALESCE(s.price, 0) AS price,
                   COALESCE(
                       direct_provider.name,
                       (
                           SELECT GROUP_CONCAT(assigned_provider.name, ', ')
                           FROM providers assigned_provider
                           WHERE assigned_provider.active = 1
                             AND assigned_provider.selected_service_id = s.id
                       ),
                       ''
                   ) AS provider_name,
                   COALESCE(
                       (
                           SELECT pay.state
                           FROM payments pay
                           WHERE pay.client_id = c.id
                             AND pay.service_id = c.selected_service_id
                           ORDER BY pay.id DESC
                           LIMIT 1
                       ),
                       'Pendente'
                   ) AS payment_state
            FROM clients c
            LEFT JOIN services s ON s.id = c.selected_service_id
            LEFT JOIN providers direct_provider ON direct_provider.id = s.provider_id
            WHERE c.active = 1
              AND c.name LIKE ?
            ORDER BY c.name
            """,
            (term,),
        )

        for row in rows:
            price = float(row["price"] or 0)
            provider_value = price * 0.7
            luz_dourada_value = price * 0.3
            self.tree.insert(
                "",
                END,
                values=(
                    row["client_name"],
                    row["provider_name"],
                    row["aula_name"],
                    format_currency(price),
                    format_currency(provider_value),
                    format_currency(luz_dourada_value),
                    row["payment_state"],
                ),
            )

    def on_tree_shift_mousewheel(self, event):
        self.tree.xview_scroll(int(-1 * (event.delta / 120)), "units")


class ScrollableEntityWindow(ttk.Frame):
    def __init__(self, parent, table, title, fields, reload_callback=None):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = Canvas(self, bg=BG_COLOR, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(
            yscrollcommand=vertical_scrollbar.set,
        )

        self.content = EntityTab(self.canvas, table, title, fields, reload_callback)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self.update_scroll_region)
        self.canvas.bind("<Configure>", self.update_canvas_width)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.content.bind("<MouseWheel>", self.on_mousewheel)
        self.after(0, self.update_scroll_region)

    def update_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def update_canvas_width(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)
        self.update_scroll_region()

    def on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class LoginDialog:
    def __init__(self, root):
        self.root = root
        self.user = None
        self.username_var = StringVar(value="admin")
        self.password_var = StringVar()

        root.title("Login")
        root.geometry("380x230")
        root.resizable(False, False)
        root.configure(bg=BG_COLOR)
        root.protocol("WM_DELETE_WINDOW", self.cancel)

        frame = ttk.Frame(root, padding=18)
        self.frame = frame
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI Semibold", 15)).pack(anchor="w")
        ttk.Label(frame, text="Indique o utilizador e password.", style="Muted.TLabel").pack(anchor="w", pady=(4, 14))

        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Utilizador").grid(row=0, column=0, sticky="w", pady=4)
        username_entry = ttk.Entry(form, textvariable=self.username_var)
        username_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=4)
        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w", pady=4)
        password_entry = ttk.Entry(form, textvariable=self.password_var, show="*")
        password_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="Entrar", command=self.login, style="Primary.TButton").pack(side="left")
        ttk.Button(actions, text="Sair", command=self.cancel).pack(side="left", padx=(8, 0))

        root.bind("<Return>", lambda _event: self.login())
        self.center()
        password_entry.focus_set()
        root.mainloop()

    def center(self):
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.lift()
        self.root.focus_force()

    def login(self):
        username = self.username_var.get().strip()
        password = self.password_var.get()
        rows = query_all("SELECT * FROM users WHERE username = ? AND active = 1", (username,))
        if not rows or not verify_password(password, rows[0]["password_hash"]):
            messagebox.showwarning("Login invalido", "Utilizador ou password invalido.")
            return
        self.user = dict(rows[0])
        self.root.quit()

    def cancel(self):
        self.user = None
        self.root.quit()


class UsersTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=16)
        self.selected_id = None
        self.username_var = StringVar()
        self.password_var = StringVar()
        self.full_name_var = StringVar()
        self.active_var = BooleanVar(value=True)
        self.search_var = StringVar()
        self.permission_vars = {key: BooleanVar(value=False) for key, _label in USER_PERMISSIONS}
        self.build()
        self.load_rows()

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        form = ttk.LabelFrame(self, text="Utilizadores", padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Utilizador").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", padx=(8, 16), pady=4)
        ttk.Label(form, text="Nome").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.full_name_var).grid(row=0, column=3, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(form, text="Password").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.password_var, show="*").grid(row=1, column=1, sticky="ew", padx=(8, 16), pady=4)
        ttk.Checkbutton(form, text="Ativo", variable=self.active_var).grid(row=1, column=3, sticky="w", pady=4)

        permissions = ttk.LabelFrame(form, text="Acessos", padding=8)
        permissions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        for index, (key, label) in enumerate(USER_PERMISSIONS):
            ttk.Checkbutton(permissions, text=label, variable=self.permission_vars[key]).grid(
                row=index // 4,
                column=index % 4,
                sticky="w",
                padx=(0, 18),
                pady=3,
            )

        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        ttk.Button(actions, text="Novo", command=self.clear_form).pack(side="left")
        ttk.Button(actions, text="Guardar", command=self.save, style="Primary.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Inativar", command=self.deactivate).pack(side="left", padx=(8, 0))
        ttk.Label(actions, text="Pesquisar").pack(side="left", padx=(24, 6))
        search = ttk.Entry(actions, textvariable=self.search_var, width=28)
        search.pack(side="left")
        search.bind("<KeyRelease>", lambda _event: self.load_rows())

        columns = ("username", "full_name", "permissions", "active")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=13)
        headings = {
            "username": "Utilizador",
            "full_name": "Nome",
            "permissions": "Acessos",
            "active": "Ativo",
        }
        widths = {
            "username": 160,
            "full_name": 200,
            "permissions": 520,
            "active": 70,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=80, stretch=False)
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        vertical_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        vertical_scrollbar.grid(row=2, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        horizontal_scrollbar.grid(row=3, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical_scrollbar.set, xscrollcommand=horizontal_scrollbar.set)

    def load_rows(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        term = f"%{self.search_var.get().strip()}%"
        rows = query_all(
            """
            SELECT *
            FROM users
            WHERE username LIKE ? OR full_name LIKE ?
            ORDER BY active DESC, username
            """,
            (term, term),
        )
        for row in rows:
            permissions = ", ".join(label for key, label in USER_PERMISSIONS if row[key])
            self.tree.insert(
                "",
                END,
                iid=str(row["id"]),
                values=(row["username"], row["full_name"], permissions, "Sim" if row["active"] else "Nao"),
            )

    def clear_form(self):
        self.selected_id = None
        self.username_var.set("")
        self.password_var.set("")
        self.full_name_var.set("")
        self.active_var.set(True)
        for var in self.permission_vars.values():
            var.set(False)
        self.tree.selection_remove(self.tree.selection())

    def save(self):
        username = self.username_var.get().strip()
        if not username:
            messagebox.showwarning("Campo obrigatorio", "Indique o utilizador.")
            return
        values = {
            "username": username,
            "full_name": self.full_name_var.get().strip(),
            "active": 1 if self.active_var.get() else 0,
        }
        for key, _label in USER_PERMISSIONS:
            values[key] = 1 if self.permission_vars[key].get() else 0

        password = self.password_var.get()
        if password:
            values["password_hash"] = hash_password(password)
        elif not self.selected_id:
            messagebox.showwarning("Campo obrigatorio", "Indique uma password para o novo utilizador.")
            return

        try:
            if self.selected_id:
                keys = list(values.keys())
                assignments = ", ".join(f"{key} = ?" for key in keys)
                execute(
                    f"UPDATE users SET {assignments} WHERE id = ?",
                    [values[key] for key in keys] + [self.selected_id],
                )
            else:
                keys = list(values.keys())
                placeholders = ", ".join("?" for _ in keys)
                execute(
                    f"INSERT INTO users ({', '.join(keys)}) VALUES ({placeholders})",
                    [values[key] for key in keys],
                )
        except sqlite3.IntegrityError:
            messagebox.showwarning("Utilizador repetido", "Ja existe um utilizador com esse nome.")
            return

        self.load_rows()
        self.clear_form()

    def deactivate(self):
        if not self.selected_id:
            messagebox.showinfo("Sem selecao", "Escolha um utilizador para inativar.")
            return
        execute("UPDATE users SET active = 0 WHERE id = ?", (self.selected_id,))
        self.load_rows()
        self.clear_form()

    def on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        row = query_all("SELECT * FROM users WHERE id = ?", (self.selected_id,))[0]
        self.username_var.set(row["username"])
        self.password_var.set("")
        self.full_name_var.set(row["full_name"])
        self.active_var.set(bool(row["active"]))
        for key, _label in USER_PERMISSIONS:
            self.permission_vars[key].set(bool(row[key]))


class App:
    def __init__(self, root, current_user):
        self.root = root
        self.current_user = current_user
        self.windows = {}
        self.sync_in_progress = False
        self.sync_status_var = StringVar(value="")
        self.root.title(APP_TITLE)
        self.root.geometry("620x420")
        self.root.minsize(520, 360)
        self.configure_styles()
        self.build()
        self.maximize_root()
        self.disable_root_maximize()

    def configure_styles(self):
        self.root.configure(bg=BG_COLOR)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 9), background=BG_COLOR, foreground=TEXT_COLOR)
        style.configure("TFrame", background=BG_COLOR)
        style.configure("Surface.TFrame", background=SURFACE_COLOR)
        style.configure("Hero.TFrame", background=PRIMARY_COLOR)
        style.configure("TLabel", background=BG_COLOR, foreground=TEXT_COLOR)
        style.configure("Surface.TLabel", background=SURFACE_COLOR, foreground=TEXT_COLOR)
        style.configure("HeroTitle.TLabel", background=PRIMARY_COLOR, foreground="#ffffff", font=("Segoe UI Semibold", 20))
        style.configure("HeroText.TLabel", background=PRIMARY_COLOR, foreground="#d1fae5", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG_COLOR, foreground=TEXT_COLOR, font=("Segoe UI Semibold", 16))
        style.configure("Muted.TLabel", background=BG_COLOR, foreground=MUTED_COLOR, font=("Segoe UI", 10))
        style.configure("TNotebook", background=BG_COLOR, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI Semibold", 9))
        style.configure(
            "Treeview",
            rowheight=28,
            background=SURFACE_COLOR,
            fieldbackground=SURFACE_COLOR,
            foreground=TEXT_COLOR,
            bordercolor=BORDER_COLOR,
        )
        style.configure("Treeview.Heading", background=SURFACE_ALT_COLOR, foreground=TEXT_COLOR, font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#ccfbf1")], foreground=[("selected", "#134e4a")])
        style.configure("TLabelframe", background=BG_COLOR, bordercolor=BORDER_COLOR, padding=8)
        style.configure("TLabelframe.Label", background=BG_COLOR, foreground=TEXT_COLOR, font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=SURFACE_COLOR, bordercolor=BORDER_COLOR, padding=(7, 5))
        style.configure("TCombobox", fieldbackground=SURFACE_COLOR, bordercolor=BORDER_COLOR, padding=(7, 5))
        style.configure("TButton", padding=(12, 7), font=("Segoe UI Semibold", 9))
        style.configure("Primary.TButton", background=PRIMARY_COLOR, foreground="#ffffff", borderwidth=0, padding=(14, 9))
        style.map(
            "Primary.TButton",
            background=[("active", PRIMARY_HOVER_COLOR), ("disabled", "#94a3b8")],
            foreground=[("disabled", "#e2e8f0")],
        )
        style.configure("Module.TButton", background=SURFACE_COLOR, foreground=TEXT_COLOR, padding=(18, 16), font=("Segoe UI Semibold", 11))
        style.map("Module.TButton", background=[("active", "#ecfeff")], foreground=[("active", PRIMARY_COLOR)])
        style.configure("Danger.TButton", background="#fff1f2", foreground="#be123c", padding=(12, 7))

    def maximize_root(self):
        try:
            self.root.state("zoomed")
        except Exception:
            self.root.geometry("1200x800")

    def disable_root_maximize(self):
        if sys.platform != "win32":
            return
        try:
            self.root.update_idletasks()
            hwnd = windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            style = windll.user32.GetWindowLongW(hwnd, -16)
            style |= 0x00020000
            style &= ~0x00010000
            windll.user32.SetWindowLongW(hwnd, -16, style)
            windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except Exception:
            pass

    def build(self):
        self.build_menu()

        main = ttk.Frame(self.root, padding=22)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        hero = ttk.Frame(main, style="Hero.TFrame", padding=(18, 16))
        hero.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 18))
        hero.columnconfigure(0, weight=1)
        ttk.Label(hero, text=APP_TITLE, style="HeroTitle.TLabel").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(
            hero,
            text=f"Utilizador: {self.current_user['username']}",
            style="HeroText.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        buttons = [
            ("Clientes", "can_clients", self.open_clients),
            ("Prestadores", "can_providers", self.open_providers),
            ("Servicos", "can_services", self.open_services),
            ("Marcar Consulta", "can_agenda", self.open_agenda),
            ("Pagamentos", "can_payments", self.open_payments),
            ("Utilizadores", "can_users", self.open_users),
            ("Sincronizar Neon", "can_sync", self.start_neon_sync),
            ("Backup", "can_backup", self.open_backup),
        ]
        visible_buttons = [(text, command) for text, permission, command in buttons if self.has_access(permission)]
        for index, (text, command) in enumerate(visible_buttons):
            button = ttk.Button(main, text=text, command=command, style="Module.TButton")
            button.grid(row=1 + index // 2, column=index % 2, sticky="ew", padx=8, pady=8)
            if text == "Sincronizar Neon":
                self.sync_button = button

        footer_row = 1 + ((len(visible_buttons) + 1) // 2)
        ttk.Label(main, textvariable=self.sync_status_var, style="Muted.TLabel").grid(
            row=footer_row,
            column=0,
            columnspan=2,
            sticky="w",
            padx=8,
            pady=(8, 0),
        )
        ttk.Separator(main).grid(row=footer_row + 1, column=0, columnspan=2, sticky="ew", pady=(20, 12))
        ttk.Button(main, text="Fechar todas as janelas", command=self.close_child_windows).grid(
            row=footer_row + 2,
            column=0,
            sticky="ew",
            padx=8,
        )
        ttk.Button(main, text="Sair", command=self.root.destroy, style="Danger.TButton").grid(
            row=footer_row + 2,
            column=1,
            sticky="ew",
            padx=8,
        )
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def build_menu(self):
        menu_bar = Menu(self.root)
        modules_menu = Menu(menu_bar, tearoff=False)
        if self.has_access("can_clients"):
            modules_menu.add_command(label="Clientes", command=self.open_clients)
        if self.has_access("can_providers"):
            modules_menu.add_command(label="Prestadores", command=self.open_providers)
        if self.has_access("can_services"):
            modules_menu.add_command(label="Servicos", command=self.open_services)
        modules_menu.add_separator()
        if self.has_access("can_agenda"):
            modules_menu.add_command(label="Marcar Consulta", command=self.open_agenda)
        if self.has_access("can_payments"):
            modules_menu.add_command(label="Pagamentos", command=self.open_payments)
        if self.has_access("can_users"):
            modules_menu.add_command(label="Utilizadores", command=self.open_users)
        if self.has_access("can_sync"):
            modules_menu.add_command(label="Sincronizar Neon", command=self.start_neon_sync)
        if self.has_access("can_backup"):
            modules_menu.add_command(label="Backup", command=self.open_backup)
        modules_menu.add_separator()
        modules_menu.add_command(label="Fechar todas", command=self.close_child_windows)
        modules_menu.add_command(label="Sair", command=self.root.destroy)
        menu_bar.add_cascade(label="Modulos", menu=modules_menu)
        self.root.config(menu=menu_bar)

    def has_access(self, permission):
        return bool(self.current_user.get(permission))

    def require_access(self, permission):
        if self.has_access(permission):
            return True
        messagebox.showwarning("Sem permissao", "O seu utilizador nao tem acesso a esta opcao.")
        return False

    def open_module_window(self, key, title, size, builder):
        existing = self.windows.get(key)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return existing

        window = Toplevel(self.root)
        window.title(title)
        window.geometry(size)
        window.minsize(700, 460)
        window.configure(bg=BG_COLOR)
        window.transient(self.root)
        content = builder(window)
        content.pack(fill="both", expand=True)
        window.protocol("WM_DELETE_WINDOW", lambda: self.close_window(key))
        self.windows[key] = window
        self.maximize_child_window(window)
        if hasattr(content, "window_size_var"):
            content.window_size_var.set("Restaurar")
        window.lift()
        return window

    def maximize_child_window(self, window):
        try:
            window.state("zoomed")
        except Exception:
            screen_width = window.winfo_screenwidth()
            screen_height = window.winfo_screenheight()
            window.geometry(f"{screen_width}x{screen_height}+0+0")

    def close_window(self, key):
        window = self.windows.pop(key, None)
        if window and window.winfo_exists():
            window.destroy()

    def close_child_windows(self):
        for key in list(self.windows):
            self.close_window(key)

    def start_neon_sync(self):
        if not self.require_access("can_sync"):
            return
        if self.sync_in_progress:
            messagebox.showinfo("Sincronizacao em curso", "A sincronizacao com o Neon ainda esta a decorrer.")
            return
        self.sync_in_progress = True
        self.sync_status_var.set("A sincronizar com o Neon...")
        if hasattr(self, "sync_button"):
            self.sync_button.configure(state="disabled")
        threading.Thread(target=self.run_neon_sync, daemon=True).start()

    def run_neon_sync(self):
        try:
            sync_to_neon.load_env_file()
            payload = sync_to_neon.build_payload()
            result = sync_to_neon.send_payload(payload)
        except Exception as exc:
            self.root.after(0, lambda: self.finish_neon_sync(None, exc))
            return
        self.root.after(0, lambda: self.finish_neon_sync(result, None))

    def finish_neon_sync(self, result, error):
        self.sync_in_progress = False
        if hasattr(self, "sync_button"):
            self.sync_button.configure(state="normal")
        if error:
            self.sync_status_var.set("Erro ao sincronizar com o Neon.")
            messagebox.showerror(
                "Erro na sincronizacao",
                f"Nao foi possivel sincronizar com o Neon.\n\nConfirme que a API esta ligada.\n\nDetalhe: {error}",
            )
            return

        counts = result.get("synced", {})
        summary = "\n".join(f"{table}: {count}" for table, count in counts.items())
        self.sync_status_var.set(f"Ultima sincronizacao Neon: {datetime.now():%d/%m/%Y %H:%M}")
        messagebox.showinfo("Sincronizacao concluida", f"Dados enviados para o Neon.\n\n{summary}")

    def open_clients(self):
        if not self.require_access("can_clients"):
            return
        self.open_module_window(
            "clients",
            "Clientes",
            "1180x780",
            lambda parent: ScrollableEntityWindow(
                parent,
                "clients",
                "Clientes",
                [
                    ("name", "Nome", "text"),
                    ("phone", "Telefone", "text"),
                    ("email", "Email", "text"),
                    ("notes", "Notas", "notes"),
                ],
                self.refresh_reference_windows,
            ),
        )

    def open_providers(self):
        if not self.require_access("can_providers"):
            return
        self.open_module_window(
            "providers",
            "Prestadores",
            "920x680",
            lambda parent: EntityTab(
                parent,
                "providers",
                "Prestadores",
                [
                    ("name", "Nome", "text"),
                    ("phone", "Telefone", "text"),
                    ("email", "Email", "text"),
                    ("selected_service_id", "Servico", "service_combo"),
                    ("notes", "Notas", "notes"),
                ],
                self.refresh_reference_windows,
            ),
        )

    def open_services(self):
        if not self.require_access("can_services"):
            return
        self.open_module_window(
            "services",
            "Servicos",
            "920x680",
            lambda parent: EntityTab(
                parent,
                "services",
                "Servicos",
                [
                    ("type", "Tipo", "combo"),
                    ("name", "Nome", "text"),
                    ("provider_id", "Prestador", "provider_combo"),
                    ("price", "Preco", "text"),
                    ("notes", "Notas", "notes"),
                ],
                self.refresh_reference_windows,
            ),
        )

    def open_agenda(self):
        if not self.require_access("can_agenda"):
            return
        self.open_module_window("agenda", "Agenda", "1400x900", lambda parent: AgendaTab(parent))

    def open_payments(self):
        if not self.require_access("can_payments"):
            return
        self.open_module_window("payments", "Pagamentos", "980x640", lambda parent: PaymentsTab(parent, self.refresh_reference_windows))

    def open_users(self):
        if not self.require_access("can_users"):
            return
        self.open_module_window("users", "Utilizadores", "980x640", lambda parent: UsersTab(parent))

    def open_backup(self):
        if not self.require_access("can_backup"):
            return
        self.open_module_window("backup", "Backup", "620x320", lambda parent: BackupTab(parent))

    def reload_agenda_windows(self):
        agenda_window = self.windows.get("agenda")
        if not agenda_window or not agenda_window.winfo_exists():
            return
        for child in agenda_window.winfo_children():
            if isinstance(child, AgendaTab):
                child.reload_options()
                child.load_week()

    def refresh_reference_windows(self):
        self.reload_agenda_windows()
        self.reload_payment_windows()
        for key in ("clients", "providers", "services"):
            window = self.windows.get(key)
            if not window or not window.winfo_exists():
                continue
            for child in self.entity_tabs_in(window):
                if isinstance(child, EntityTab) and child.table == "clients":
                    child.reload_client_aula_options()
                    child.load_rows()
                if isinstance(child, EntityTab) and child.table == "providers":
                    child.reload_service_options()
                    child.load_rows()
                if isinstance(child, EntityTab) and child.table == "services":
                    child.reload_provider_options()
                    child.load_rows()

    def reload_payment_windows(self):
        payment_window = self.windows.get("payments")
        if not payment_window or not payment_window.winfo_exists():
            return
        for child in self.payment_tabs_in(payment_window):
            child.reload_options()
            child.load_rows()

    def entity_tabs_in(self, widget):
        tabs = []
        for child in widget.winfo_children():
            if isinstance(child, EntityTab):
                tabs.append(child)
            else:
                tabs.extend(self.entity_tabs_in(child))
        return tabs

    def payment_tabs_in(self, widget):
        tabs = []
        for child in widget.winfo_children():
            if isinstance(child, PaymentsTab):
                tabs.append(child)
            else:
                tabs.extend(self.payment_tabs_in(child))
        return tabs


def main():
    init_db()
    root = Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BG_COLOR)
    style.configure("TLabel", background=BG_COLOR, foreground=TEXT_COLOR)
    style.configure("Muted.TLabel", background=BG_COLOR, foreground=MUTED_COLOR, font=("Segoe UI", 10))
    style.configure("Primary.TButton", background=PRIMARY_COLOR, foreground="#ffffff", borderwidth=0, padding=(14, 9))
    login = LoginDialog(root)
    if not login.user:
        root.destroy()
        return
    root.unbind("<Return>")
    for child in root.winfo_children():
        child.destroy()
    root.resizable(True, True)
    App(root, login.user)
    root.mainloop()


if __name__ == "__main__":
    main()
