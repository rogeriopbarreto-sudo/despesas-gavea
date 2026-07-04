"""Dashboard: classificação de categorias e pré-computação noturna (1h BRT)."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import db
from notify import get_anthropic

_TZ = ZoneInfo("America/Sao_Paulo")
_MODEL = "claude-sonnet-4-6"
_BATCH = 120

CATEGORIES = ["Alimentos", "Bebidas", "Limpeza", "Higiene e Farmácia", "Móveis",
              "Itens da Casa", "Material de Construção", "Ferramentas",
              "Elétrica e Hidráulica", "Jardim", "Pet", "Combustível", "Outros"]


def _classify(names: list[str]) -> dict[str, str]:
    prompt = (
        "Classifique cada produto na categoria mais adequada desta lista fixa:\n"
        f"{', '.join(CATEGORIES)}\n\n"
        "Responda APENAS com um objeto JSON {\"<produto>\": \"<categoria>\"} — sem "
        "texto antes/depois, sem markdown. Produtos:\n"
        + "\n".join(f"- {n}" for n in names)
    )
    msg = get_anthropic().messages.create(
        model=_MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    text = text.replace("```json", "").replace("```", "").strip()
    mapping = json.loads(text)
    return {n: c for n, c in mapping.items() if c in CATEGORIES}


def classify_new_canonicals() -> int:
    pending = db.uncategorized_canonicals()
    done = 0
    for i in range(0, len(pending), _BATCH):
        batch = pending[i:i + _BATCH]
        try:
            mapping = _classify(batch)
        except Exception as e:
            print(f"[Dash] Classificação falhou ({e}) — lote pulado.", flush=True)
            continue
        db.save_categories(mapping)
        done += len(mapping)
    return done


def refresh() -> dict:
    classified = classify_new_canonicals()
    today = datetime.now(_TZ).date()
    payload = db.compute_dash(today)
    db.set_dash_cache("dash", payload)
    return {"classified": classified, "date": payload["date"]}


def get() -> dict | None:
    payload = db.get_dash_cache("dash")
    if payload:
        payload["audit"] = db.get_dash_cache("audit_summary")  # sempre fresco, fora do cache de 1h
    return payload
