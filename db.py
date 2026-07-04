"""Camada de dados PostgreSQL (substitui sheets.py a partir da v3.0)."""
import base64
import functools
import json
import os
import uuid
from datetime import date as _date
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

WORKSPACES = ("main", "obra")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
    id              uuid PRIMARY KEY,
    workspace       text NOT NULL DEFAULT 'main',
    store           text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    value           numeric NOT NULL,
    date            date,
    payment_method  text NOT NULL DEFAULT '',
    thumb           bytea,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reimbursable    boolean NOT NULL DEFAULT false,
    reimb_done      boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS items (
    id              uuid PRIMARY KEY,
    expense_id      uuid NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
    product_name    text NOT NULL DEFAULT '',
    canonical_name  text NOT NULL DEFAULT '',
    unit            text NOT NULL DEFAULT 'un',
    unit_price      numeric NOT NULL DEFAULT 0,
    total_price     numeric NOT NULL DEFAULT 0,
    store           text NOT NULL DEFAULT '',
    date            date,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS receipts (
    expense_id  uuid PRIMARY KEY REFERENCES expenses(id) ON DELETE CASCADE,
    photo       bytea NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    store           text NOT NULL,
    raw_text        text NOT NULL,
    corrected_name  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (store, raw_text)
);
CREATE TABLE IF NOT EXISTS categories (
    canonical_name  text PRIMARY KEY,
    category        text NOT NULL
);
CREATE TABLE IF NOT EXISTS dash_cache (
    key         text PRIMARY KEY,
    payload     jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          uuid PRIMARY KEY,
    expense_id  uuid,
    workspace   text NOT NULL DEFAULT 'main',
    severity    text NOT NULL,
    flags       jsonb NOT NULL,
    verdict     text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_canonical ON items (upper(canonical_name));
CREATE INDEX IF NOT EXISTS idx_items_expense ON items (expense_id);
CREATE INDEX IF NOT EXISTS idx_expenses_ws_date ON expenses (workspace, date);
"""


@functools.lru_cache(maxsize=1)
def _pool() -> ConnectionPool:
    return ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5,
                          kwargs={"row_factory": dict_row})


def init_schema():
    with _pool().connection() as conn:
        conn.execute(_SCHEMA)


def _q(sql: str, params=()) -> list[dict]:
    with _pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def _q1(sql: str, params=()) -> dict | None:
    rows = _q(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params=()) -> int:
    with _pool().connection() as conn:
        return conn.execute(sql, params).rowcount


def _b64_to_bytes(b64: str) -> bytes | None:
    if not b64:
        return None
    if "," in b64:  # data URL: "data:image/jpeg;base64,XXXX"
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def _iso(v) -> str:
    return v.isoformat() if v is not None else ""


# ── Expenses ─────────────────────────────────────────────────────────────────

def append_expense(data: dict, photo_b64: str = "", workspace: str = "main") -> str:
    expense_id = str(uuid.uuid4())
    thumb = _b64_to_bytes(data.get("thumb_b64", ""))
    photo = _b64_to_bytes(photo_b64)
    with _pool().connection() as conn:
        conn.execute(
            """INSERT INTO expenses (id, workspace, store, description, value, date,
                                     payment_method, thumb, reimbursable)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (expense_id, workspace, data.get("store", ""), data.get("description", ""),
             data.get("value", 0), data.get("date") or None,
             data.get("payment_method", ""), thumb, bool(data.get("reimbursable"))))
        if photo:
            conn.execute("INSERT INTO receipts (expense_id, photo) VALUES (%s,%s)",
                         (expense_id, photo))
        items = data.get("items", [])
        if items:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO items (id, expense_id, product_name, canonical_name,
                                          unit, unit_price, total_price, store, date)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(str(uuid.uuid4()), expense_id, it.get("name", ""),
                      it.get("canonical_name", ""), it.get("unit", "un"),
                      it.get("unit_price", 0), it.get("total_price", 0),
                      data.get("store", ""), data.get("date") or None)
                     for it in items])
    return expense_id


def list_expenses(workspace: str = "main", month: str | None = None) -> list[dict]:
    where, params = "workspace = %s", [workspace]
    if month:
        where += " AND to_char(date, 'YYYY-MM') = %s"
        params.append(month)
    records = _q(f"""
        SELECT e.id::text, e.store, e.description, e.value::float8 AS value, e.date,
               e.payment_method, e.created_at, e.reimbursable, e.reimb_done,
               (e.thumb IS NOT NULL) AS has_thumb,
               (r.expense_id IS NOT NULL) AS has_photo
        FROM expenses e LEFT JOIN receipts r ON r.expense_id = e.id
        WHERE {where}
        ORDER BY e.date DESC NULLS LAST, e.created_at DESC""", params)

    items = _q("""
        SELECT i.expense_id::text, i.product_name AS name,
               i.unit_price::float8 AS unit_price,
               i.total_price::float8 AS total_price, i.unit
        FROM items i JOIN expenses e ON e.id = i.expense_id
        WHERE e.workspace = %s""", [workspace])
    items_map: dict[str, list] = {}
    for it in items:
        eid = it.pop("expense_id")
        items_map.setdefault(eid, []).append(it)

    for r in records:
        r["date"] = _iso(r.pop("date"))
        r["created_at"] = _iso(r.pop("created_at"))
        r["photo_file_id"] = r["id"] if r.pop("has_photo") else ""
        r["items"] = items_map.get(r["id"], [])
    return records


def get_thumb(expense_id: str) -> bytes | None:
    row = _q1("SELECT thumb FROM expenses WHERE id = %s", (expense_id,))
    return row["thumb"] if row else None


def get_receipt(expense_id: str) -> bytes | None:
    row = _q1("SELECT photo FROM receipts WHERE expense_id = %s", (expense_id,))
    return row["photo"] if row else None


def delete_expense(expense_id: str) -> bool:
    return _exec("DELETE FROM expenses WHERE id = %s", (expense_id,)) > 0


def mark_reimb_done(expense_id: str) -> bool:
    return _exec("UPDATE expenses SET reimb_done = true WHERE id = %s", (expense_id,)) > 0


# ── Price items ──────────────────────────────────────────────────────────────

def list_price_items(workspace: str = "main", q: str | None = None) -> list[dict]:
    where, params = "e.workspace = %s", [workspace]
    if q:
        where += " AND (i.product_name ILIKE %s OR i.canonical_name ILIKE %s)"
        params += [f"%{q}%", f"%{q}%"]
    rows = _q(f"""
        SELECT i.expense_id::text, i.product_name, i.canonical_name, i.unit,
               i.unit_price::float8 AS unit_price, i.total_price::float8 AS total_price,
               i.store, i.date
        FROM items i JOIN expenses e ON e.id = i.expense_id
        WHERE {where}""", params)
    for r in rows:
        r["date"] = _iso(r.pop("date"))
    return rows


# ── Corrections ──────────────────────────────────────────────────────────────

def get_corrections(store: str) -> list[dict]:
    return _q("""SELECT store, raw_text, corrected_name FROM corrections
                 WHERE upper(trim(store)) = upper(trim(%s))""", (store,))


def save_correction(store: str, raw_text: str, corrected_name: str):
    _exec("""INSERT INTO corrections (store, raw_text, corrected_name)
             VALUES (%s,%s,%s)
             ON CONFLICT (store, raw_text)
             DO UPDATE SET corrected_name = EXCLUDED.corrected_name, created_at = now()""",
          (store, raw_text, corrected_name))


# ── Auditoria ────────────────────────────────────────────────────────────────

def item_history(canonical_name: str, unit: str, exclude_expense_id: str = "") -> list[dict]:
    """Histórico de compras do mesmo produto canônico (todas as workspaces)."""
    rows = _q("""
        SELECT i.unit_price::float8 AS unit_price, i.total_price::float8 AS total_price,
               i.unit, i.store, i.date, i.product_name
        FROM items i
        WHERE upper(i.canonical_name) = upper(%s) AND i.unit = %s
          AND i.canonical_name <> '' AND i.unit_price > 0
          AND i.expense_id::text <> %s
        ORDER BY i.date""", (canonical_name, unit, exclude_expense_id))
    for r in rows:
        r["date"] = _iso(r.pop("date"))
    return rows


def find_duplicates(workspace: str, store: str, date: str, value: float,
                    exclude_id: str = "") -> list[dict]:
    rows = _q("""
        SELECT id::text, store, date, value::float8 AS value, created_at
        FROM expenses
        WHERE workspace = %s AND upper(trim(store)) = upper(trim(%s))
          AND date = %s AND abs(value - %s) < 0.01 AND id::text <> %s""",
              (workspace, store, date or None, value, exclude_id))
    for r in rows:
        r["date"], r["created_at"] = _iso(r.pop("date")), _iso(r.pop("created_at"))
    return rows


def expenses_since(days: int) -> list[dict]:
    rows = _q("""
        SELECT e.id::text, e.workspace, e.store, e.description,
               e.value::float8 AS value, e.date, e.payment_method, e.reimbursable,
               (e.thumb IS NOT NULL) AS has_thumb,
               (SELECT count(*) FROM items i WHERE i.expense_id = e.id)::int AS n_items
        FROM expenses e
        WHERE e.created_at >= now() - make_interval(days => %s)
        ORDER BY e.created_at""", (days,))
    for r in rows:
        r["date"] = _iso(r.pop("date"))
    return rows


def audit_log_add(expense_id: str, workspace: str, severity: str,
                  flags: list[dict], verdict: str):
    _exec("""INSERT INTO audit_log (id, expense_id, workspace, severity, flags, verdict)
             VALUES (%s,%s,%s,%s,%s,%s)""",
          (str(uuid.uuid4()), expense_id or None, workspace, severity, Jsonb(flags), verdict))


def audit_log_since(days: int) -> list[dict]:
    rows = _q("""
        SELECT a.severity, a.flags, a.verdict, a.workspace, a.created_at,
               e.store, e.value::float8 AS value, e.date
        FROM audit_log a LEFT JOIN expenses e ON e.id = a.expense_id
        WHERE a.created_at >= now() - make_interval(days => %s)
        ORDER BY a.created_at""", (days,))
    for r in rows:
        r["date"], r["created_at"] = _iso(r.pop("date")), _iso(r.pop("created_at"))
    return rows


def price_creep(min_weeks: int = 3) -> list[dict]:
    """Produtos com preço médio semanal subindo há >= min_weeks semanas seguidas."""
    rows = _q("""
        SELECT upper(canonical_name) AS canon, min(canonical_name) AS name, unit,
               date_trunc('week', date)::date AS week,
               avg(unit_price)::float8 AS avg_price
        FROM items
        WHERE canonical_name <> '' AND unit_price > 0 AND date >= current_date - 70
        GROUP BY 1, 3, 4 ORDER BY 1, 4""")
    by_item: dict[tuple, list] = {}
    for r in rows:
        by_item.setdefault((r["canon"], r["unit"]), []).append(r)
    creeping = []
    for (canon, unit), series in by_item.items():
        if len(series) < min_weeks + 1:
            continue
        tail = series[-(min_weeks + 1):]
        prices = [s["avg_price"] for s in tail]
        if all(b > a for a, b in zip(prices, prices[1:])):
            creeping.append({"name": tail[0]["name"], "unit": unit,
                             "from": round(prices[0], 2), "to": round(prices[-1], 2),
                             "pct": round((prices[-1] / prices[0] - 1) * 100, 1),
                             "weeks": len(prices) - 1})
    return creeping


def new_stores(days: int = 7) -> list[str]:
    return [r["store"] for r in _q("""
        SELECT store, min(created_at) AS first_seen FROM expenses
        WHERE store <> '' GROUP BY store
        HAVING min(created_at) >= now() - make_interval(days => %s)""", (days,))]


# ── Categorias e Dash ────────────────────────────────────────────────────────

def uncategorized_canonicals() -> list[str]:
    return [r["name"] for r in _q("""
        SELECT DISTINCT i.canonical_name AS name FROM items i
        LEFT JOIN categories c ON upper(c.canonical_name) = upper(i.canonical_name)
        WHERE i.canonical_name <> '' AND c.canonical_name IS NULL
        ORDER BY 1""")]


def save_categories(pairs: dict[str, str]):
    if not pairs:
        return
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO categories (canonical_name, category)
                               VALUES (%s,%s) ON CONFLICT (canonical_name)
                               DO UPDATE SET category = EXCLUDED.category""",
                            list(pairs.items()))


def set_dash_cache(key: str, payload: dict):
    _exec("""INSERT INTO dash_cache (key, payload, updated_at) VALUES (%s,%s,now())
             ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()""",
          (key, Jsonb(payload)))


def get_dash_cache(key: str) -> dict | None:
    row = _q1("SELECT payload, updated_at FROM dash_cache WHERE key = %s", (key,))
    if not row:
        return None
    payload = row["payload"]
    payload["generated_at"] = _iso(row["updated_at"])
    return payload


def compute_dash(today: _date) -> dict:
    """Agregados do dashboard: totais, por loja, por categoria, séries."""
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)

    def totals(start: _date, end: _date) -> dict:
        rows = _q("""SELECT workspace, coalesce(sum(value),0)::float8 AS total,
                            count(*)::int AS n
                     FROM expenses WHERE date >= %s AND date <= %s
                     GROUP BY workspace""", (start, end))
        by_ws = {r["workspace"]: r for r in rows}
        return {"total": round(sum(r["total"] for r in rows), 2),
                "n": sum(r["n"] for r in rows),
                "main": round(by_ws.get("main", {}).get("total", 0), 2),
                "obra": round(by_ws.get("obra", {}).get("total", 0), 2)}

    by_store = _q("""
        SELECT store, workspace,
               coalesce(sum(value) FILTER (WHERE date >= %s), 0)::float8 AS month_total,
               count(*) FILTER (WHERE date >= %s)::int AS month_n,
               coalesce(sum(value) FILTER (WHERE date >= %s AND date <= %s), 0)::float8 AS prev_month_total
        FROM expenses
        WHERE store <> '' AND date >= %s
        GROUP BY store, workspace
        HAVING coalesce(sum(value) FILTER (WHERE date >= %s), 0) > 0
            OR coalesce(sum(value) FILTER (WHERE date >= %s AND date <= %s), 0) > 0
        ORDER BY 3 DESC""",
        (month_start, month_start, prev_month_start, prev_month_end,
         prev_month_start, month_start, prev_month_start, prev_month_end))

    by_category = _q("""
        SELECT coalesce(c.category, 'Sem categoria') AS category,
               coalesce(sum(i.total_price) FILTER (WHERE i.date >= %s), 0)::float8 AS month_total,
               coalesce(sum(i.total_price) FILTER (WHERE i.date >= %s), 0)::float8 AS ytd_total
        FROM items i
        LEFT JOIN categories c ON upper(c.canonical_name) = upper(i.canonical_name)
        WHERE i.date >= %s
        GROUP BY 1 ORDER BY 3 DESC""", (month_start, year_start, year_start))

    # top itens (por valor gasto no ano) dentro de cada categoria — drill-down do Dash
    cat_items = _q("""
        SELECT coalesce(c.category, 'Sem categoria') AS category,
               coalesce(nullif(i.canonical_name, ''), i.product_name) AS name,
               sum(i.total_price)::float8 AS total, count(*)::int AS n
        FROM items i
        LEFT JOIN categories c ON upper(c.canonical_name) = upper(i.canonical_name)
        WHERE i.date >= %s AND i.total_price > 0
        GROUP BY 1, 2 ORDER BY 3 DESC""", (year_start,))
    top_by_cat: dict[str, list] = {}
    for r in cat_items:
        lst = top_by_cat.setdefault(r["category"], [])
        if len(lst) < 5:
            lst.append({"name": r["name"], "total": round(r["total"], 2), "n": r["n"]})
    for c in by_category:
        c["items"] = top_by_cat.get(c["category"], [])

    by_payment = _q("""
        SELECT coalesce(nullif(payment_method, ''), 'Não informado') AS method,
               coalesce(sum(value) FILTER (WHERE date >= %s), 0)::float8 AS month_total,
               coalesce(sum(value) FILTER (WHERE date >= %s), 0)::float8 AS ytd_total,
               count(*) FILTER (WHERE date >= %s)::int AS ytd_n
        FROM expenses WHERE date >= %s
        GROUP BY 1 HAVING coalesce(sum(value) FILTER (WHERE date >= %s), 0) > 0
        ORDER BY 3 DESC""",
        (month_start, year_start, year_start, year_start, year_start))

    weekly = _q("""
        SELECT date_trunc('week', date)::date AS week, workspace,
               sum(value)::float8 AS total
        FROM expenses WHERE date >= %s - interval '12 weeks' AND date IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1""", (week_start,))
    monthly = _q("""
        SELECT to_char(date, 'YYYY-MM') AS month, workspace, sum(value)::float8 AS total
        FROM expenses WHERE date >= %s - interval '12 months' AND date IS NOT NULL
        GROUP BY 1, 2 ORDER BY 1""", (month_start,))

    for r in weekly:
        r["week"] = _iso(r.pop("week"))
    for r in by_store + by_category + by_payment + weekly + monthly:
        for k, v in list(r.items()):
            if isinstance(v, float):
                r[k] = round(v, 2)

    return {
        "date": today.isoformat(),
        "today": totals(today, today),
        "week": totals(week_start, today),
        "month": totals(month_start, today),
        "ytd": totals(year_start, today),
        "by_store": by_store,
        "by_category": by_category,
        "by_payment": by_payment,
        "weekly_series": weekly,
        "monthly_series": monthly,
    }
