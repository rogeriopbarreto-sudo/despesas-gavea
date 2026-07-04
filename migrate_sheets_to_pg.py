"""Migração única Google Sheets → PostgreSQL (v3.0).

Lê as abas expenses/price_items/corrections/receipts e insere tudo no Postgres.
A planilha NÃO é alterada (só leitura) — fica como arquivo histórico.

Uso:  python migrate_sheets_to_pg.py          (precisa de GOOGLE_SHEETS_CREDENTIALS,
      GOOGLE_SPREADSHEET_ID e DATABASE_URL no .env)
"""
import base64
import functools
import json
import os
import uuid

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

import db  # noqa: E402 — precisa do .env carregado

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@functools.lru_cache(maxsize=1)
def _spreadsheet() -> gspread.Spreadsheet:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    if "private_key" in creds_dict:
        pk = creds_dict["private_key"]
        creds_dict["private_key"] = pk.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(os.environ["GOOGLE_SPREADSHEET_ID"])


def _records(title: str) -> list[dict]:
    try:
        return _spreadsheet().worksheet(title).get_all_records()
    except gspread.WorksheetNotFound:
        return []


def _b64_bytes(b64: str) -> bytes | None:
    if not b64:
        return None
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def _bool(v) -> bool:
    return str(v).strip().lower() == "true"


def main():
    db.init_schema()
    expenses = _records("expenses")
    items = _records("price_items")
    corrections = _records("corrections")

    receipts_ws = None
    try:
        receipts_ws = _spreadsheet().worksheet("receipts")
    except gspread.WorksheetNotFound:
        pass
    receipt_rows = receipts_ws.get_all_values()[1:] if receipts_ws else []
    receipts = {row[0]: "".join(row[1:]) for row in receipt_rows if row and row[0]}

    with db._pool().connection() as conn:
        n_exp = 0
        for e in expenses:
            eid = str(e.get("id", "")).strip()
            if not eid:
                continue
            conn.execute(
                """INSERT INTO expenses (id, workspace, store, description, value, date,
                                         payment_method, thumb, created_at, reimbursable, reimb_done)
                   VALUES (%s,'main',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (eid, e.get("store", ""), e.get("description", ""),
                 float(e.get("value") or 0), str(e.get("date", "")).strip() or None,
                 e.get("payment_method", ""), _b64_bytes(str(e.get("thumb_b64", ""))),
                 str(e.get("created_at", "")).strip() or None,
                 _bool(e.get("reimbursable")), _bool(e.get("reimb_done"))))
            n_exp += 1

            photo = _b64_bytes(receipts.get(eid, ""))
            if photo:
                conn.execute("""INSERT INTO receipts (expense_id, photo) VALUES (%s,%s)
                                ON CONFLICT (expense_id) DO NOTHING""", (eid, photo))

        expense_ids = {str(e.get("id", "")).strip() for e in expenses}
        n_items = n_orphans = 0
        for it in items:
            eid = str(it.get("expense_id", "")).strip()
            if eid not in expense_ids:  # item de despesa já apagada — sem FK possível
                n_orphans += 1
                continue
            conn.execute(
                """INSERT INTO items (id, expense_id, product_name, canonical_name, unit,
                                      unit_price, total_price, store, date, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (id) DO NOTHING""",
                (str(it.get("id", "")).strip() or str(uuid.uuid4()), eid,
                 it.get("product_name", ""), it.get("canonical_name", ""),
                 it.get("unit", "un") or "un", float(it.get("unit_price") or 0),
                 float(it.get("total_price") or 0), it.get("store", ""),
                 str(it.get("date", "")).strip() or None,
                 str(it.get("created_at", "")).strip() or None))
            n_items += 1

        n_corr = 0
        for c in corrections:
            if not str(c.get("store", "")).strip() and not str(c.get("raw_text", "")).strip():
                continue
            conn.execute(
                """INSERT INTO corrections (store, raw_text, corrected_name, created_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (store, raw_text) DO NOTHING""",
                (c.get("store", ""), str(c.get("raw_text", "")),
                 c.get("corrected_name", ""),
                 str(c.get("created_at", "")).strip() or None))
            n_corr += 1

    counts = db._q1("""SELECT (SELECT count(*) FROM expenses)::int AS expenses,
                              (SELECT count(*) FROM items)::int AS items,
                              (SELECT count(*) FROM corrections)::int AS corrections,
                              (SELECT count(*) FROM receipts)::int AS receipts""")
    print("── Conferência origem (Sheets) × destino (Postgres) ──")
    print(f"expenses:    {len(expenses):>5}  →  {counts['expenses']:>5}")
    print(f"price_items: {len(items):>5}  →  {counts['items']:>5}"
          + (f"  ({n_orphans} órfãos ignorados — despesa-mãe já apagada)" if n_orphans else ""))
    print(f"corrections: {len(corrections):>5}  →  {counts['corrections']:>5}")
    print(f"receipts:    {len(receipts):>5}  →  {counts['receipts']:>5}")
    db._pool().close()


if __name__ == "__main__":
    main()
