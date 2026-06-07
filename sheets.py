import functools
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

EXPENSES_HEADERS    = ["id", "store", "description", "value", "date", "payment_method", "thumb_b64", "created_at", "reimbursable", "reimb_done"]
ITEMS_HEADERS       = ["id", "expense_id", "product_name", "unit_price", "unit", "store", "date", "created_at", "total_price"]
CORRECTIONS_HEADERS = ["store", "raw_text", "corrected_name", "created_at"]

_REIMB_DONE_COL = EXPENSES_HEADERS.index("reimb_done") + 1  # 1-indexed = 10


@functools.lru_cache(maxsize=1)
def _get_client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    if "private_key" in creds_dict:
        pk = creds_dict["private_key"]
        creds_dict["private_key"] = pk.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


@functools.lru_cache(maxsize=1)
def _get_spreadsheet() -> gspread.Spreadsheet:
    return _get_client().open_by_key(os.environ["GOOGLE_SPREADSHEET_ID"])


def _open_or_create(title: str, headers: list[str]) -> gspread.Worksheet:
    ss = _get_spreadsheet()
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws
    if not ws.row_values(1):
        ws.append_row(headers)
    return ws


@functools.lru_cache(maxsize=1)
def _expenses_ws() -> gspread.Worksheet:
    return _open_or_create("expenses", EXPENSES_HEADERS)


@functools.lru_cache(maxsize=1)
def _items_ws() -> gspread.Worksheet:
    return _open_or_create("price_items", ITEMS_HEADERS)


@functools.lru_cache(maxsize=1)
def _corrections_ws() -> gspread.Worksheet:
    return _open_or_create("corrections", CORRECTIONS_HEADERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SheetsDB:
    def __init__(self):
        self._expenses_ws    = _expenses_ws()
        self._items_ws       = _items_ws()
        self._corrections_ws = _corrections_ws()

    def migrate(self):
        """Adiciona colunas ausentes introduzidas em versões posteriores."""
        exp_hdrs = self._expenses_ws.row_values(1)
        for col_name in ("reimbursable", "reimb_done"):
            if col_name not in exp_hdrs:
                col = len(exp_hdrs) + 1
                self._expenses_ws.resize(rows=1000, cols=col)
                self._expenses_ws.update_cell(1, col, col_name)
                exp_hdrs.append(col_name)

        item_hdrs = self._items_ws.row_values(1)
        if "total_price" not in item_hdrs:
            col = len(item_hdrs) + 1
            self._items_ws.resize(rows=1000, cols=col)
            self._items_ws.update_cell(1, col, "total_price")

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
            _now(),
            "true" if data.get("reimbursable") else "false",
            "false",
        ]
        self._expenses_ws.append_row(row, value_input_option="RAW")

        item_rows = [
            [
                str(uuid.uuid4()),
                expense_id,
                item.get("name", ""),
                str(item.get("unit_price", 0)),
                item.get("unit", "un"),
                data.get("store", ""),
                data.get("date", ""),
                _now(),
                str(item.get("total_price", 0)),
            ]
            for item in data.get("items", [])
        ]
        if item_rows:
            self._items_ws.append_rows(item_rows, value_input_option="RAW")

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
                "name":        it.get("product_name", ""),
                "unit_price":  it.get("unit_price", 0),
                "total_price": it.get("total_price", 0),
                "unit":        it.get("unit", "un"),
            })
        for r in records:
            r["items"] = items_map.get(str(r.get("id", "")), [])

        return records

    def delete_expense(self, expense_id: str) -> bool:
        cell = self._expenses_ws.find(expense_id, in_column=1)
        if not cell:
            return False
        self._expenses_ws.delete_rows(cell.row)

        all_items = self._items_ws.get_all_values()
        rows_to_delete = [
            i + 1
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
        seen: dict[str, tuple[str, int]] = {}
        for r in records:
            name = str(r.get("product_name", "")).strip()
            if not name:
                continue
            key = name.upper()
            if key in seen:
                seen[key] = (seen[key][0], seen[key][1] + 1)
            else:
                seen[key] = (name, 1)
        return [seen[k][0] for k in sorted(seen, key=lambda k: seen[k][1], reverse=True)[:limit]]

    def get_corrections(self, store: str) -> list[dict]:
        store_key = store.upper().strip()
        return [
            r for r in self._corrections_ws.get_all_records()
            if str(r.get("store", "")).upper().strip() == store_key
        ]

    def mark_reimb_done(self, expense_id: str) -> bool:
        cell = self._expenses_ws.find(expense_id, in_column=1)
        if not cell:
            return False
        self._expenses_ws.update_cell(cell.row, _REIMB_DONE_COL, "true")
        return True

    def save_correction(self, store: str, raw_text: str, corrected_name: str):
        records = self._corrections_ws.get_all_records()
        for i, r in enumerate(records):
            if (str(r.get("store", "")).upper().strip() == store.upper().strip() and
                    str(r.get("raw_text", "")).upper().strip() == raw_text.upper().strip()):
                row_idx = i + 2
                self._corrections_ws.update_cell(row_idx, 3, corrected_name)
                self._corrections_ws.update_cell(row_idx, 4, _now())
                return
        self._corrections_ws.append_row(
            [store, raw_text, corrected_name, _now()],
            value_input_option="RAW",
        )
