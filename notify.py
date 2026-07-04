"""Serviços compartilhados: Telegram e cliente Anthropic."""
import functools
import html
import json
import os
import urllib.request

import anthropic

PIX_PHONE = "21-97064-2002"  # número para receber reembolso via Pix

_TG_CHUNK = 4000  # limite do Telegram é 4096 chars por mensagem


@functools.lru_cache(maxsize=1)
def get_anthropic() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def fmt_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def tg_send(text: str, chat_id: str | None = None, parse_mode: str | None = "HTML"):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not all([token, chat_id]):
        print("[Telegram] Credenciais ausentes — envio ignorado.", flush=True)
        return
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"[Telegram] Enviado: {resp.status}", flush=True)
    except Exception as e:
        print(f"[Telegram] Erro: {e}", flush=True)


def tg_send_long(lines: list[str], chat_id: str | None = None, parse_mode: str | None = "HTML"):
    """Divide em mensagens sequenciais respeitando o limite por linha."""
    chunks, cur, cur_len = [], [], 0
    for line in lines:
        if cur and cur_len + len(line) + 1 > _TG_CHUNK:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    for chunk in chunks:
        tg_send(chunk, chat_id=chat_id, parse_mode=parse_mode)


def audit_chat_id() -> str | None:
    return os.environ.get("TELEGRAM_AUDIT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")


def send_expense_notification(store: str, description: str, value: float,
                              date: str, payment_method: str,
                              reimbursable: bool, items: list, workspace: str = "main"):
    date_fmt = "/".join(reversed(date.split("-"))) if date else ""
    pay_icon = {"Cartão de Crédito": "💳", "Vale Alimentação": "🍽️", "Pix": "📱"}.get(payment_method, "")
    pay_str = f" · {pay_icon} {payment_method}" if payment_method else ""

    title = "🏗️ <b>Obra — nova despesa</b>" if workspace == "obra" else "🧾 <b>Nova despesa</b>"
    lines = [title, f"🏪 <b>{html.escape(store or 'Sem estabelecimento')}</b>"]
    if description and description != store:
        lines.append(f"📝 {html.escape(description)}")
    lines.append(f"📅 {date_fmt}{pay_str}")
    lines.append(f"<b>Total: {fmt_brl(value)}</b>")
    if reimbursable:
        lines.append("⚠️ <b>SOLICITA REEMBOLSO</b>")

    if items:
        lines.append("")
        for it in items:
            price = float(it.get("total_price") or it.get("unit_price") or 0)
            lines.append(f"• {html.escape(str(it.get('name', '?')))} — {fmt_brl(price)}")

    tg_send_long(lines)

    if reimbursable:
        tg_send(f"faz um pix de {fmt_brl(value)} para {PIX_PHONE}", parse_mode=None)
