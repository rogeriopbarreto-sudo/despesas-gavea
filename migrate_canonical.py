"""Migração única: preenche canonical_name nos price_items já gravados no Google Sheets.

A unidade NÃO é alterada no histórico: as linhas antigas têm unit_price igual ao total
da linha (sem peso/volume real), então convertê-las para kg/L produziria preços
unitários errados. Só notas novas trazem R$/kg e R$/L corretos.

Uso:
    python migrate_canonical.py            # dry-run: mostra o mapeamento, não grava
    python migrate_canonical.py --apply    # grava no Google Sheets
"""

import json
import os
import sys

import anthropic
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

BATCH = 80  # nomes por chamada à IA

PROMPT = (
    "Você recebe uma lista JSON de produtos de cupons fiscais brasileiros, cada um com "
    '"name" (texto do cupom) e "unit" (un, kg ou L).\n'
    "Para cada produto, devolva o nome canônico: o nome genérico que agrupa compras do mesmo "
    "produto em lojas diferentes — sem marca, embalagem, peso ou abreviação. Exemplos:\n"
    "  'Queijo Prato Pre Fat' → 'Queijo Prato'\n"
    "  'Gasolina Aditivada Shell' → 'Gasolina Aditivada'\n"
    "  'Leite Int Italac 1L' → 'Leite Integral'\n"
    "  'Ovos Ver Cai Mant' → 'Ovos Caipira'\n"
    "Corrija também a unidade quando estiver claramente errada: combustíveis e líquidos "
    "vendidos por litro devem ter unit 'L'; itens vendidos por peso, 'kg'; o resto mantém a atual.\n"
    "Responda APENAS com um array JSON, sem texto antes/depois, sem markdown, no formato:\n"
    '[{"name":"<original>","canonical_name":"<canônico>","unit":"<un, kg ou L>"}]\n\n'
    "Lista:\n"
)


def classify(client: anthropic.Anthropic, names: list[dict]) -> dict[str, dict]:
    """name original → {canonical_name, unit}"""
    out: dict[str, dict] = {}
    for i in range(0, len(names), BATCH):
        chunk = names[i:i + BATCH]
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            messages=[{"role": "user", "content": PROMPT + json.dumps(chunk, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        for row in json.loads(text):
            unit = row.get("unit", "un")
            out[row["name"]] = {
                "canonical_name": str(row.get("canonical_name", "")).strip(),
                "unit": unit if unit in ("un", "kg", "L") else "un",
            }
    return out


def main():
    apply = "--apply" in sys.argv

    from sheets import SheetsDB, _items_ws
    SheetsDB().migrate()  # garante a coluna canonical_name
    ws = _items_ws()

    values = ws.get_all_values()
    headers = values[0]
    col_name  = headers.index("product_name")
    col_unit  = headers.index("unit")
    col_canon = headers.index("canonical_name")

    pending = []  # (row_idx 1-based, product_name, unit)
    for i, row in enumerate(values[1:], start=2):
        name  = row[col_name].strip() if len(row) > col_name else ""
        canon = row[col_canon].strip() if len(row) > col_canon else ""
        unit  = row[col_unit].strip() if len(row) > col_unit else "un"
        if name and not canon:
            pending.append((i, name, unit or "un"))

    if not pending:
        print("Nada a migrar — todos os itens já têm canonical_name.")
        return

    unique = {}
    for _, name, unit in pending:
        unique.setdefault(name, unit)
    print(f"{len(pending)} linhas sem canonical_name ({len(unique)} nomes únicos). Consultando IA…\n")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    mapping = classify(client, [{"name": n, "unit": u} for n, u in unique.items()])

    width = max(len(n) for n in unique)
    for name, unit in unique.items():
        m = mapping.get(name)
        if not m:
            print(f"  {name:<{width}}  →  (sem resposta — ficará vazio)")
            continue
        print(f"  {name:<{width}}  →  {m['canonical_name']}")

    if not apply:
        print("\nDry-run: nada gravado. Rode com --apply para gravar no Sheets.")
        return

    cells = []
    import gspread
    for row_idx, name, unit in pending:
        m = mapping.get(name)
        if not m:
            continue
        cells.append(gspread.Cell(row_idx, col_canon + 1, m["canonical_name"]))
    if cells:
        ws.update_cells(cells, value_input_option="RAW")
    print(f"\n✓ {len(cells)} células atualizadas no Sheets.")


if __name__ == "__main__":
    main()
