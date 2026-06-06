import json
import os
import threading
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

APP_VERSION = "1.9"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sheets import SheetsDB, _get_client
    _get_client()  # warm up auth cache on startup
    db = SheetsDB()
    db._migrate_items_headers()
    db._migrate_expenses_headers()
    db._migrate_reimb_done()
    yield


app = FastAPI(title="Despesas Gávea", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Telegram notification ─────────────────────────────────────────────────────

def _fmt_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def send_telegram_notification(store: str, description: str, value: float,
                               date: str, payment_method: str,
                               reimbursable: bool, items: list):
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not all([token, chat_id]):
        print("[Telegram] Credenciais ausentes — notificação ignorada.", flush=True)
        return

    date_fmt = "/".join(reversed(date.split("-"))) if date else ""
    pay_icon = {"Cartão de Crédito": "💳", "Vale Alimentação": "🍽️", "Pix": "📱"}.get(payment_method, "")
    pay_str = f" · {pay_icon} {payment_method}" if payment_method else ""

    lines = ["🧾 <b>Nova despesa</b>",
             f"🏪 <b>{store or 'Sem estabelecimento'}</b>"]
    if description and description != store:
        lines.append(f"📝 {description}")
    lines.append(f"📅 {date_fmt}{pay_str}")
    lines.append(f"<b>Total: {_fmt_brl(value)}</b>")
    if reimbursable:
        lines.append("⚠️ <b>SOLICITA REEMBOLSO</b>")

    MAX_ITEMS = 5
    shown = items[:MAX_ITEMS]
    if shown:
        lines.append("")
        for it in shown:
            price = float(it.get("total_price") or it.get("unit_price") or 0)
            lines.append(f"• {it.get('name', '?')} — {_fmt_brl(price)}")
        if len(items) > MAX_ITEMS:
            lines.append(f"<i>{len(items) - MAX_ITEMS} itens a mais</i>")

    body = "\n".join(lines)
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": body, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"[Telegram] Enviado: {resp.status}", flush=True)
    except Exception as e:
        print(f"[Telegram] Erro: {e}", flush=True)


# Lazy-init DB per request to avoid cold-start auth errors on startup
def get_db():
    from sheets import SheetsDB
    return SheetsDB()


# ── Pydantic models ──────────────────────────────────────────────────────────

class ReadReceiptRequest(BaseModel):
    image_base64: str
    media_type: str = "image/jpeg"
    brief_desc: str = ""


class ItemIn(BaseModel):
    name: str
    unit_price: float
    total_price: float = 0
    unit: str = "un"


class ExpenseIn(BaseModel):
    store: str = ""
    description: str = ""
    value: float
    date: str
    payment_method: str = ""
    reimbursable: bool = False
    thumb_b64: str = ""
    items: list[ItemIn] = []


class CorrectionIn(BaseModel):
    store: str
    raw_text: str
    corrected_name: str


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/version")
def version():
    return {"version": APP_VERSION}


@app.get("/api/health")
def health():
    try:
        from sheets import _get_client
        _get_client()  # reuses cached client; verifies credentials are loadable
        return {"status": "ok", "sheets": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Auth error: {e}")


@app.post("/api/read-receipt")
def read_receipt(req: ReadReceiptRequest):
    today = __import__("datetime").date.today().isoformat()
    prompt = (
        "Você está lendo a foto de um cupom fiscal de supermercado ou recibo brasileiro.\n"
        "Responda APENAS com um objeto JSON, sem texto antes/depois, sem markdown.\n"
        'Formato exato:\n{"store":"<nome curto e padronizado>","value":<total pago, ponto decimal>,'
        '"date":"<AAAA-MM-DD>","payment_method":"<ver regras>","items":[{"name":"<produto>",'
        '"unit":"<un ou kg>","unit_price":<VL.UNIT>,"total_price":<VL.TOTAL>}]}\n\n'
        "store: nome comercial + bairro/filial curto. Ex: 'Zona Sul - Leblon'.\n"
        "value: VALOR A PAGAR após descontos.\n"
        "items: unit_price = PREÇO UNITÁRIO (VL.UNIT); total_price = TOTAL DA LINHA (VL.TOTAL). unit=kg se vendido por peso.\n"
        "payment_method — somente 3 casos, senão vazio:\n"
        "  crédito/credit → 'Cartão de Crédito'\n"
        "  vale/voucher/débito → 'Vale Alimentação'\n"
        "  dinheiro/espécie/Pix → 'Pix'\n"
        "  qualquer outra coisa ou incerteza → ''\n"
        f"Se não achar a data use '{today}'.\n"
        "ITENS DUVIDOSOS: Se o nome de um produto for muito abreviado, ilegível ou ambíguo, "
        'inclua nos campos do item: "uncertain": true e "raw_text": "<texto exato do cupom>".'
    )
    if req.brief_desc:
        prompt += f' A pessoa descreveu como: "{req.brief_desc}".'

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": req.media_type,
                        "data": req.image_base64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    text = "".join(b.text for b in message.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Não consegui interpretar a resposta da IA.")


@app.post("/api/expenses", status_code=201)
def create_expense(exp: ExpenseIn):
    db = get_db()
    expense_id = db.append_expense(exp.model_dump())
    threading.Thread(
        target=send_telegram_notification,
        args=(exp.store, exp.description, exp.value,
              exp.date, exp.payment_method, exp.reimbursable,
              [i.model_dump() for i in exp.items]),
        daemon=True,
    ).start()
    return {"id": expense_id}


@app.get("/api/expenses")
def list_expenses(month: str | None = None):
    db = get_db()
    return db.list_expenses(month=month)


@app.patch("/api/expenses/{expense_id}/reimb-done", status_code=200)
def mark_reimb_done(expense_id: str):
    db = get_db()
    found = db.mark_reimb_done(expense_id)
    if not found:
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    return {"ok": True}


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: str):
    db = get_db()
    found = db.delete_expense(expense_id)
    if not found:
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    return {"ok": True}


@app.get("/api/prices")
def list_prices(q: str | None = None):
    db = get_db()
    items = db.list_price_items(q=q)

    groups: dict[str, dict] = {}
    for item in items:
        name = str(item.get("product_name", "")).strip()
        unit = str(item.get("unit", "un"))
        key = f"{name.upper()}|{unit}"
        if key not in groups:
            groups[key] = {"name": name, "unit": unit, "entries": []}
        try:
            price = float(item.get("unit_price", 0))
        except (ValueError, TypeError):
            continue
        groups[key]["entries"].append({
            "store": item.get("store", ""),
            "price": price,
            "date": item.get("date", ""),
        })

    result = []
    for g in groups.values():
        entries = sorted(g["entries"], key=lambda e: e["price"])
        result.append({"name": g["name"], "unit": g["unit"], "entries": entries})
    return result


@app.get("/api/prices/suggestions")
def price_suggestions():
    db = get_db()
    return db.price_suggestions()


@app.get("/api/corrections")
def list_corrections(store: str | None = None):
    if not store:
        return []
    db = get_db()
    return db.get_corrections(store)


@app.post("/api/corrections", status_code=201)
def save_corrections(corrections: list[CorrectionIn]):
    if not corrections:
        return {"ok": True}
    db = get_db()
    for c in corrections:
        db.save_correction(c.store, c.raw_text, c.corrected_name)
    return {"ok": True}
