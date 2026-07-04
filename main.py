import hashlib
import hmac
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import date as _date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auditor
import dash
import db
from notify import get_anthropic, send_expense_notification

load_dotenv()

APP_VERSION = "3.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    yield


app = FastAPI(title="Despesas Gávea", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── Auth: PIN one-time por aparelho, token HMAC por escopo ───────────────────

_PIN_ENVS = {"main": "ACCESS_PIN_MAIN", "obra": "ACCESS_PIN_OBRA", "dash": "ACCESS_PIN_DASH"}


def _scope_token(scope: str) -> str:
    secret = os.environ.get("SECRET_KEY", "dev-secret")
    return hmac.new(secret.encode(), f"ws:{scope}".encode(), hashlib.sha256).hexdigest()


def _pin_for(scope: str) -> str:
    return os.environ.get(_PIN_ENVS[scope], "")


def _require(scope: str, x_auth: str | None):
    """Valida o token do escopo. Casa e obra são isoladas entre si (um funcionário
    não vê o ambiente do outro), MAS o token do dono (dash) abre casa e obra —
    é ele que alterna Casa|Obra e vê tudo. Sem PIN no ambiente, o escopo fica aberto."""
    if not _pin_for(scope):
        return
    valid = [_scope_token(scope)]
    if scope in ("main", "obra"):
        valid.append(_scope_token("dash"))
    if not x_auth or not any(hmac.compare_digest(x_auth, t) for t in valid):
        raise HTTPException(status_code=401, detail="Não autorizado.")


def _ws(ws: str) -> str:
    if ws not in db.WORKSPACES:
        raise HTTPException(status_code=400, detail="Workspace inválido.")
    return ws


class AuthIn(BaseModel):
    pin: str
    scope: str


@app.post("/api/auth")
def auth(body: AuthIn):
    if body.scope not in _PIN_ENVS:
        raise HTTPException(status_code=400, detail="Escopo inválido.")
    pin = _pin_for(body.scope)
    if not pin:
        return {"token": _scope_token(body.scope)}
    if not hmac.compare_digest(body.pin.strip(), pin):
        raise HTTPException(status_code=401, detail="PIN incorreto.")
    return {"token": _scope_token(body.scope)}


# ── Pydantic models ──────────────────────────────────────────────────────────

class ReadReceiptRequest(BaseModel):
    image_base64: str
    media_type: str = "image/jpeg"
    brief_desc: str = ""


class ItemIn(BaseModel):
    name: str
    canonical_name: str = ""
    unit_price: float
    total_price: float = 0
    unit: str = "un"


class ExpenseIn(BaseModel):
    store: str = ""
    description: str = ""
    value: float = Field(gt=0)
    date: str
    payment_method: str = ""
    reimbursable: bool = False
    thumb_b64: str = ""
    photo_b64: str = ""
    items: list[ItemIn] = []


class CorrectionIn(BaseModel):
    store: str
    raw_text: str
    corrected_name: str


# ── Páginas ──────────────────────────────────────────────────────────────────

def _page(workspace: str) -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html.replace("__WORKSPACE__", workspace))


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("main")


@app.get("/obra", response_class=HTMLResponse)
def obra():
    return _page("obra")


@app.get("/api/version")
def version():
    return {"version": APP_VERSION}


@app.get("/api/health")
def health():
    try:
        db.init_schema()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"DB error: {e}")


# ── Leitura de nota (IA) ─────────────────────────────────────────────────────

@app.post("/api/read-receipt")
def read_receipt(req: ReadReceiptRequest, ws: str = Query("main"),
                 x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    today = _date.today().isoformat()
    prompt = (
        "Você está lendo a foto de um cupom fiscal de supermercado, loja de material "
        "de construção ou recibo brasileiro.\n"
        "Responda APENAS com um objeto JSON, sem texto antes/depois, sem markdown.\n"
        'Formato exato:\n{"store":"<nome curto e padronizado>","value":<total pago, ponto decimal>,'
        '"date":"<AAAA-MM-DD>","payment_method":"<ver regras>","items":[{"name":"<produto>",'
        '"canonical_name":"<produto genérico>","unit":"<un, kg ou L>",'
        '"unit_price":<VL.UNIT>,"total_price":<VL.TOTAL>}]}\n\n'
        "store: nome comercial + bairro/filial curto. Ex: 'Zona Sul - Leblon'.\n"
        "value: VALOR A PAGAR após descontos.\n"
        "items: unit_price = PREÇO UNITÁRIO (VL.UNIT); total_price = TOTAL DA LINHA (VL.TOTAL).\n"
        "unit: kg se vendido por peso; L se vendido por litro (combustível, líquidos a granel); senão un.\n"
        "canonical_name: nome genérico do produto para agrupar compras iguais de lojas diferentes — "
        "sem marca, embalagem ou abreviação. Ex: 'Queijo Prato Pre Fat' → 'Queijo Prato'; "
        "'Gasolina Aditivada Shell' → 'Gasolina Comum' apenas se for comum, senão 'Gasolina Aditivada'; "
        "'Leite Int Italac 1L' → 'Leite Integral'; 'Cimento CP II 50kg Votoran' → 'Cimento'.\n"
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

    message = get_anthropic().messages.create(
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


# ── Despesas ─────────────────────────────────────────────────────────────────

@app.post("/api/expenses", status_code=201)
def create_expense(exp: ExpenseIn, ws: str = Query("main"),
                   x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    data = exp.model_dump(exclude={"photo_b64"})
    expense_id = db.append_expense(data, photo_b64=exp.photo_b64, workspace=ws)
    items = [i.model_dump() for i in exp.items]
    threading.Thread(
        target=send_expense_notification,
        args=(exp.store, exp.description, exp.value, exp.date,
              exp.payment_method, exp.reimbursable, items, ws),
        daemon=True,
    ).start()
    threading.Thread(
        target=auditor.run_audit, args=(expense_id, data, items, ws),
        daemon=True,
    ).start()
    return {"id": expense_id}


@app.get("/api/expenses")
def list_expenses(month: str | None = None, ws: str = Query("main"),
                  x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    return db.list_expenses(workspace=ws, month=month)


@app.patch("/api/expenses/{expense_id}/reimb-done", status_code=200)
def mark_reimb_done(expense_id: str, ws: str = Query("main"),
                    x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    if not db.mark_reimb_done(expense_id):
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    return {"ok": True}


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: str, ws: str = Query("main"),
                   x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    if not db.delete_expense(expense_id):
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    return {"ok": True}


# Miniatura e foto ficam sem header de auth: são carregadas via <img src> e o id
# uuid é a capability (não enumerável).

@app.get("/api/thumb/{expense_id}")
def get_thumb(expense_id: str):
    thumb = db.get_thumb(expense_id)
    if not thumb:
        raise HTTPException(status_code=404, detail="Miniatura não encontrada.")
    return Response(content=thumb, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/receipt/{receipt_id}")
def get_receipt(receipt_id: str):
    content = db.get_receipt(receipt_id)
    if not content:
        raise HTTPException(status_code=404, detail="Foto não encontrada.")
    return Response(content=content, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── Preços ───────────────────────────────────────────────────────────────────

@app.get("/api/prices")
def list_prices(q: str | None = None, ws: str = Query("main"),
                x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    items = db.list_price_items(workspace=ws, q=q)

    # canônico+unidade → grupo; dentro do grupo, descrição+loja+data → entry única
    # (itens idênticos comprados juntos viram 1 entry com quantidade somada)
    groups: dict[str, dict] = {}
    for item in items:
        desc = str(item.get("product_name", "")).strip()
        canonical = str(item.get("canonical_name", "")).strip() or desc
        unit = str(item.get("unit", "un")).strip() or "un"
        key = f"{canonical.upper()}|{unit}"
        if key not in groups:
            groups[key] = {"name": canonical, "unit": unit, "entries": {}}
        unit_price = float(item.get("unit_price") or 0)
        line_total = float(item.get("total_price") or 0) or unit_price
        store, date = item.get("store", ""), item.get("date", "")
        entry = groups[key]["entries"].setdefault(
            (desc.upper(), store, date),
            {"desc": desc, "store": store, "date": date, "qty": 0.0, "total": 0.0, "expense_id": ""})
        if not entry["expense_id"]:  # vínculo p/ abrir a nota — só o id, sem imagem
            entry["expense_id"] = str(item.get("expense_id", "") or "")
        # qty em unidades p/ "un"; em peso/volume (total ÷ preço unitário) p/ kg e L
        if unit in ("kg", "L") and unit_price > 0:
            entry["qty"] += line_total / unit_price
        else:
            entry["qty"] += 1
        entry["total"] += line_total

    result = []
    for g in groups.values():
        entries = []
        for e in g["entries"].values():
            e["total"] = round(e["total"], 2)
            qty = e["qty"] or 1
            e["qty"] = round(qty, 3)
            e["price"] = round(e["total"] / qty, 2)  # preço por un/kg/L p/ comparação
            entries.append(e)
        entries.sort(key=lambda e: e["date"], reverse=True)  # mais recente primeiro
        result.append({"name": g["name"], "unit": g["unit"], "entries": entries})
    return result


# ── Correções ────────────────────────────────────────────────────────────────

@app.get("/api/corrections")
def list_corrections(store: str | None = None, ws: str = Query("main"),
                     x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    if not store:
        return []
    return db.get_corrections(store)


@app.post("/api/corrections", status_code=201)
def save_corrections(corrections: list[CorrectionIn], ws: str = Query("main"),
                     x_auth: str | None = Header(None)):
    _require(_ws(ws), x_auth)
    for c in corrections:
        db.save_correction(c.store, c.raw_text, c.corrected_name)
    return {"ok": True}


# ── Auditoria e Dash (agendados via Coolify Scheduled Task) ─────────────────

def _check_key(key: str | None):
    secret = os.environ.get("AUDIT_SECRET", "")
    if not secret or not key or not hmac.compare_digest(key, secret):
        raise HTTPException(status_code=403, detail="Proibido.")


@app.post("/api/audit/weekly")
def audit_weekly(key: str | None = None):
    _check_key(key)
    return auditor.weekly_digest()


@app.post("/api/dash/refresh")
def dash_refresh(key: str | None = None):
    _check_key(key)
    return dash.refresh()


@app.get("/api/dash")
def dash_get(x_auth: str | None = Header(None)):
    _require("dash", x_auth)
    payload = dash.get()
    if not payload:
        raise HTTPException(status_code=404, detail="Dashboard ainda não computado.")
    return payload
