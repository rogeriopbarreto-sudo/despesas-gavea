import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

app = FastAPI(title="Despesas Gávea")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
    unit: str = "un"


class ExpenseIn(BaseModel):
    store: str = ""
    description: str = ""
    value: float
    date: str
    payment_method: str = ""
    thumb_b64: str = ""
    items: list[ItemIn] = []


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.post("/api/read-receipt")
def read_receipt(req: ReadReceiptRequest):
    today = __import__("datetime").date.today().isoformat()
    prompt = (
        "Você está lendo a foto de um cupom fiscal de supermercado ou recibo brasileiro.\n"
        "Responda APENAS com um objeto JSON, sem texto antes/depois, sem markdown.\n"
        'Formato exato:\n{"store":"<nome curto e padronizado>","value":<total pago, ponto decimal>,'
        '"date":"<AAAA-MM-DD>","payment_method":"<ver regras>","items":[{"name":"<produto>",'
        '"unit":"<un ou kg>","unit_price":<preço unitário>}]}\n\n'
        "store: nome comercial + bairro/filial curto. Ex: 'Zona Sul - Leblon'.\n"
        "value: VALOR A PAGAR após descontos.\n"
        "items: PREÇO UNITÁRIO (VL.UNIT), não total da linha. unit=kg se vendido por peso.\n"
        "payment_method — somente 3 casos, senão vazio:\n"
        "  crédito/credit → 'Cartão de Crédito'\n"
        "  vale/voucher/débito → 'Vale Alimentação'\n"
        "  dinheiro/espécie/Pix → 'Pix'\n"
        "  qualquer outra coisa ou incerteza → ''\n"
        f"Se não achar a data use '{today}'."
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
    return {"id": expense_id}


@app.get("/api/expenses")
def list_expenses(month: str | None = None):
    db = get_db()
    return db.list_expenses(month=month)


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
