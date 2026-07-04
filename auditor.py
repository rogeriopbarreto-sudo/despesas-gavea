"""Auditoria silenciosa de compras — alertas SÓ via Telegram, nunca no app."""
import html
import json
import statistics

import db
from notify import audit_chat_id, fmt_brl, get_anthropic, tg_send_long

_MODEL = "claude-sonnet-4-6"

PRICE_MEDIAN_FACTOR = 1.4   # unit_price > mediana histórica × 1.4
PRICE_MAX_FACTOR    = 1.2   # unit_price > máximo histórico × 1.2
QTY_FACTOR          = 3.0   # quantidade > 3× a maior quantidade histórica
NO_ITEMS_MIN_VALUE  = 200.0 # despesa sem itens a partir deste valor
ROUND_MULTIPLE      = 50.0  # valor redondo (múltiplo de R$ 50, >= R$ 100) sem itens


def _qty(item: dict) -> float:
    unit_price = float(item.get("unit_price") or 0)
    total = float(item.get("total_price") or 0) or unit_price
    if item.get("unit") in ("kg", "L") and unit_price > 0:
        return total / unit_price
    return 1.0


def analyze_expense(expense: dict, items: list[dict], workspace: str,
                    expense_id: str = "") -> list[dict]:
    """Regras determinísticas. Retorna lista de flags (vazia = nada suspeito)."""
    flags = []

    for it in items:
        canonical = str(it.get("canonical_name") or "").strip()
        unit = str(it.get("unit") or "un")
        unit_price = float(it.get("unit_price") or 0)
        if not canonical or unit_price <= 0:
            continue
        history = db.item_history(canonical, unit, exclude_expense_id=expense_id)
        if len(history) >= 3:
            prices = [h["unit_price"] for h in history]
            med, mx = statistics.median(prices), max(prices)
            if unit_price > med * PRICE_MEDIAN_FACTOR or unit_price > mx * PRICE_MAX_FACTOR:
                flags.append({
                    "type": "preco_fora_do_padrao",
                    "item": it.get("name") or canonical, "canonical": canonical,
                    "unit": unit, "unit_price": unit_price,
                    "mediana": round(med, 2), "maximo": round(mx, 2),
                    "n_historico": len(prices),
                    "historico_recente": history[-5:],
                })
            qtys = [_qty(h | {"unit": unit}) for h in history]
            q = _qty(it)
            if q > max(qtys) * QTY_FACTOR and q > 2:
                flags.append({
                    "type": "quantidade_suspeita",
                    "item": it.get("name") or canonical, "canonical": canonical,
                    "unit": unit, "quantidade": round(q, 2),
                    "maior_historica": round(max(qtys), 2),
                })

    dups = db.find_duplicates(workspace, expense.get("store", ""),
                              expense.get("date", ""), float(expense.get("value") or 0),
                              exclude_id=expense_id)
    if dups:
        flags.append({"type": "nota_duplicada",
                      "duplicatas": [{"id": d["id"], "created_at": d["created_at"]} for d in dups]})

    value = float(expense.get("value") or 0)
    if not items:
        if value >= NO_ITEMS_MIN_VALUE:
            flags.append({"type": "sem_detalhamento", "valor": value})
        elif value >= 100 and value % ROUND_MULTIPLE == 0:
            flags.append({"type": "valor_redondo_sem_itens", "valor": value})

    if flags and expense.get("reimbursable"):
        for f in flags:
            f["reembolso_solicitado"] = True

    return flags


def _claude_verdict(expense: dict, flags: list[dict], workspace: str) -> str:
    prompt = (
        "Você é um investigador e auditor financeiro de despesas domésticas e de obra. "
        "Funcionários compram para o patrão e registram as notas; regras estatísticas "
        "flagraram a compra abaixo como fora do padrão.\n\n"
        f"Ambiente: {'obra (materiais de construção)' if workspace == 'obra' else 'casa'}\n"
        f"Compra: {json.dumps(expense, ensure_ascii=False, default=str)}\n"
        f"Flags: {json.dumps(flags, ensure_ascii=False, default=str)}\n\n"
        "Escreva um parecer CURTO (máx. 6 linhas) em português para o dono, por Telegram:\n"
        "1ª linha: veredito '🟡 ATENÇÃO' ou '🔴 INVESTIGAR' + resumo de uma frase.\n"
        "Depois: o que exatamente está fora do padrão (números concretos, preço unitário "
        "vs. histórico) e o que verificar na prática (ex: conferir a foto da nota, "
        "perguntar onde foi comprado). Tom factual de investigador; aponte o desvio sem "
        "acusar ninguém diretamente. Sem markdown, só texto puro."
    )
    msg = get_anthropic().messages.create(
        model=_MODEL, max_tokens=400,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def run_audit(expense_id: str, expense: dict, items: list[dict], workspace: str):
    """Roda em thread daemon após salvar a despesa. Nunca propaga erro ao app."""
    try:
        flags = analyze_expense(expense, items, workspace, expense_id=expense_id)
        store = expense.get("store") or "?"
        val = fmt_brl(float(expense.get("value") or 0))
        env = "obra" if workspace == "obra" else "casa"
        if not flags:
            db.set_dash_cache("audit_summary", {
                "status": "ok",
                "text": f"Última compra analisada ({env}): {store}, {val} — dentro do padrão. Nenhuma anomalia.",
                "scope": "compra",
            })
            return
        try:
            verdict = _claude_verdict(expense, flags, workspace)
        except Exception as e:
            print(f"[Audit] Claude indisponível ({e}) — alerta com regras puras.", flush=True)
            verdict = "🟡 ATENÇÃO (parecer automático indisponível)\n" + "\n".join(
                f"- {f['type']}: {json.dumps(f, ensure_ascii=False, default=str)[:200]}" for f in flags)

        severity = "alta" if verdict.startswith("🔴") else "media"
        db.audit_log_add(expense_id, workspace, severity, flags, verdict)

        env = "🏗️ Obra" if workspace == "obra" else "🏠 Casa"
        lines = ["🕵️ <b>AUDITORIA — compra fora do padrão</b>",
                 f"{env} · 🏪 {html.escape(expense.get('store') or '?')} · "
                 f"<b>{fmt_brl(float(expense.get('value') or 0))}</b> · "
                 f"{expense.get('date', '')}",
                 "", html.escape(verdict)]
        tg_send_long(lines, chat_id=audit_chat_id())

        db.set_dash_cache("audit_summary", {
            "status": "alerta" if severity == "alta" else "atencao",
            "text": f"{store} · {val} ({env}) — {verdict.splitlines()[0] if verdict else 'compra fora do padrão'}",
            "scope": "compra",
        })
    except Exception as e:
        print(f"[Audit] Erro: {e}", flush=True)


# ── Digest semanal ───────────────────────────────────────────────────────────

def weekly_digest():
    """Recap de auditoria dos últimos 7 dias — enviado só por Telegram."""
    log = db.audit_log_since(7)
    creep = db.price_creep()
    stores = db.new_stores(7)
    week = db.expenses_since(7)
    no_photo = [e for e in week if not e["has_thumb"]]
    no_items = [e for e in week if e["n_items"] == 0 and e["value"] >= 100]

    stats = {
        "flags_da_semana": [{k: f[k] for k in ("severity", "workspace", "store", "value", "date", "verdict")}
                            for f in log],
        "price_creep": creep,
        "fornecedores_novos": stores,
        "notas_sem_foto": [{"store": e["store"], "value": e["value"], "date": e["date"],
                            "workspace": e["workspace"]} for e in no_photo],
        "notas_sem_detalhamento": [{"store": e["store"], "value": e["value"], "date": e["date"],
                                    "workspace": e["workspace"]} for e in no_items],
        "n_despesas_semana": len(week),
    }

    prompt = (
        "Você é um investigador e auditor financeiro. Abaixo, os achados da semana na "
        "auditoria de compras da casa e da obra (funcionários compram para o patrão).\n\n"
        f"{json.dumps(stats, ensure_ascii=False, default=str)}\n\n"
        "Escreva o RELATÓRIO SEMANAL DE AUDITORIA em português, por Telegram, para o dono. "
        "Só anomalias e riscos — números de gasto normais não entram. Estrutura: uma linha "
        "de resumo executivo; depois seções curtas apenas para o que houver (alertas da "
        "semana, preços subindo semana a semana com % concreto, possíveis duplicatas, "
        "fornecedores novos, notas sem foto ou sem detalhamento). Se a semana estiver "
        "limpa, diga isso em duas linhas. Termine com no máximo 3 ações recomendadas. "
        "Tom factual de investigador, sem acusações diretas. Sem markdown, texto puro; "
        "pode usar emojis discretos como marcadores."
    )
    try:
        msg = get_anthropic().messages.create(
            model=_MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}])
        report = "".join(b.text for b in msg.content if b.type == "text").strip()
    except Exception as e:
        print(f"[Audit] Claude indisponível no digest ({e}).", flush=True)
        report = ("Relatório automático indisponível. Dados brutos:\n"
                  + json.dumps(stats, ensure_ascii=False, default=str)[:3000])

    lines = ["🕵️ <b>AUDITORIA SEMANAL — Despesas Gávea</b>", "", html.escape(report)]
    tg_send_long(lines, chat_id=audit_chat_id())

    clean = not log and not creep
    db.set_dash_cache("audit_summary", {
        "status": "ok" if clean else "atencao",
        "text": ("Auditoria semanal concluída — nenhuma anomalia relevante nos últimos 7 dias."
                 if clean else
                 f"Auditoria semanal: {len(log)} alerta(s) e {len(creep)} item(ns) com preço subindo. Detalhes no Telegram."),
        "scope": "semana",
    })
    return {"flags": len(log), "price_creep": len(creep), "sent": True}
