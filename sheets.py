import json
import os
import uuid
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

EXPENSES_HEADERS = ["id", "store", "description", "value", "date", "payment_method", "thumb_b64", "created_at", "reimbursable"]
ITEMS_HEADERS = ["id", "expense_id", "product_name", "unit_price", "unit", "store", "date", "created_at", "total_price"]


def _get_client() -> gspread.Client:
    creds_json = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
    creds_dict = json.loads(creds_json)
    # Cloud env vars (Render, Heroku, etc.) may double-escape newlines in the
    # private key, leaving literal \n instead of actual newline characters.
    if "private_key" in creds_dict:
        pk = creds_dict["private_key"]
        pk = pk.replace("\\n", "\n")   # double-escaped: \\n → \n
        pk = pk.replace("\r\n", "\n")  # Windows CRLF → \n
        pk = pk.replace("\r", "\n")    # old Mac CR → \n
        creds_dict["private_key"] = pk
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_spreadsheet() -> gspread.Spreadsheet:
    client = _get_client()
    spreadsheet_id = os.environ["GOOGLE_SPREADSHEET_ID"]
    return client.open_by_key(spreadsheet_id)


def _ensure_worksheet(spreadsheet: gspread.Spreadsheet, title: str, headers: list[str]) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws

    existing = ws.row_values(1)
    if not existing:
        ws.append_row(headers)
    return ws


class SheetsDB:
    def __init__(self):
        self._ss = _get_spreadsheet()
        self._expenses_ws = _ensure_worksheet(self._ss, "expenses", EXPENSES_HEADERS)
        self._items_ws = _ensure_worksheet(self._ss, "price_items", ITEMS_HEADERS)
        self._migrate_items_headers()
        self._migrate_expenses_headers()

    def _migrate_items_headers(self):
        existing = self._items_ws.row_values(1)
        if "total_price" not in existing:
            new_col = len(existing) + 1
            self._items_ws.resize(rows=1000, cols=new_col)
            self._items_ws.update_cell(1, new_col, "total_price")

    def _migrate_expenses_headers(self):
        existing = self._expenses_ws.row_values(1)
        if "reimbursable" not in existing:
            new_col = len(existing) + 1
            self._expenses_ws.resize(rows=1000, cols=new_col)
            self._expenses_ws.update_cell(1, new_col, "reimbursable")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def append_expense(self, data: dict) -> str:
        expense_id = str(uuid.uuid4())
        row = [
            expense_id,
            data.get("store", ""),
            data.get("description", ""),
            str(data.get("value", 0)),
            data.get("date", ""),
            data.get("payment_method", ""),
            data.get("thumb_b64", ""),
            self._now(),
            "true" if data.get("reimbursable") else "false",
        ]
        self._expenses_ws.append_row(row, value_input_option="RAW")

        for item in data.get("items", []):
            item_row = [
                str(uuid.uuid4()),
                expense_id,
                item.get("name", ""),
                str(item.get("unit_price", 0)),
                item.get("unit", "un"),
                data.get("store", ""),
                data.get("date", ""),
                self._now(),
                str(item.get("total_price", 0)),
            ]
            self._items_ws.append_row(item_row, value_input_option="RAW")

        return expense_id

    def list_expenses(self, month: str | None = None) -> list[dict]:
        records = self._expenses_ws.get_all_records()
        if month:
            records = [r for r in records if str(r.get("date", "")).startswith(month)]
        records.sort(key=lambda r: (str(r.get("date", "")), str(r.get("created_at", ""))), reverse=True)

        all_items = self._items_ws.get_all_records()
        items_map: dict[str, list] = {}
        for it in all_items:
            eid = str(it.get("expense_id", ""))
            items_map.setdefault(eid, []).append({
                "name": it.get("product_name", ""),
                "unit_price": it.get("unit_price", 0),
                "total_price": it.get("total_price", 0),
                "unit": it.get("unit", "un"),
            })
        for r in records:
            r["items"] = items_map.get(str(r.get("id", "")), [])

        return records

    def delete_expense(self, expense_id: str) -> bool:
        # delete from expenses sheet
        cell = self._expenses_ws.find(expense_id, in_column=1)
        if not cell:
            return False
        self._expenses_ws.delete_rows(cell.row)

        # delete all matching price_items rows (iterate in reverse to keep row indices valid)
        all_items = self._items_ws.get_all_values()
        rows_to_delete = [
            i + 1  # 1-indexed
            for i, row in enumerate(all_items)
            if len(row) > 1 and row[1] == expense_id
        ]
        for row_idx in sorted(rows_to_delete, reverse=True):
            self._items_ws.delete_rows(row_idx)

        return True

    def list_price_items(self, q: str | None = None) -> list[dict]:
        records = self._items_ws.get_all_records()
        if q:
            q_upper = q.upper()
            records = [r for r in records if q_upper in str(r.get("product_name", "")).upper()]
        return records

    def price_suggestions(self, limit: int = 10) -> list[str]:
        records = self._items_ws.get_all_records()
        counts: dict[str, int] = {}
        for r in records:
            name = str(r.get("product_name", "")).strip()
            if name:
                counts[name.upper()] = counts.get(name.upper(), 0) + 1
        sorted_names = sorted(counts, key=lambda k: counts[k], reverse=True)
        # return original casing from first occurrence
        seen: dict[str, str] = {}
        for r in records:
            name = str(r.get("product_name", "")).strip()
            if name and name.upper() not in seen:
                seen[name.upper()] = name
        return [seen[k] for k in sorted_names[:limit] if k in seen]
