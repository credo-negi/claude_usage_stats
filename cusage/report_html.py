"""HTML レポート生成。外部 CDN・ライブラリ不要の単体ファイル（社内ネットワーク・オフラインで開ける）。

集計済みの数値ではなく「(ターン, 日, モデル)」単位の事実表を JSON で埋め込み、ブラウザ側で
期間・プロジェクト・モデルの絞り込みに応じて再集計する。金額は Python 側で計算済みの値を
そのまま合算するだけなので、単価ロジックは Python 1 箇所に集約される。
"""
from __future__ import annotations

import json
import os
import re

from . import __version__
from .aggregate import Report

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "report.html")
TITLE = "Claude 利用状況レポート"


def _r(v: float) -> float:
    return round(v, 6)


MAX_FAMILIES = 8        # 色相の数（CSS の --s1〜--s8）
MAX_TONES = 5           # 同一系統内のトーンの数。超えたモデルは「その他」色


def model_family(model: str) -> str:
    """モデル名から系統名（opus / sonnet / haiku / fable …）を取り出す。数字・fast 表記は無視する。"""
    for tok in re.split(r"[^a-z0-9]+", model.lower()):
        if tok and tok not in ("claude", "fast") and not tok.isdigit():
            return tok
    return model


def assign_model_colors(models: list[str], weights: list[float]) -> tuple[list[int], list]:
    """系統ごとに色相、バージョン違いにトーンを割り当てる。

    models は weights（金額）の大きい順。最も使われた系統が 0 番（primary）、以降は金額順。
    系統内でも金額の大きいモデルが 0 番（基準トーン）で、以降は段階的に淡く／暗くなる。
    戻り値: (系統ごとにまとめた並び順, モデルごとの [色相番号, トーン番号]。割り当て外は None)
    """
    fam_weight: dict[str, float] = {}
    members: dict[str, list[int]] = {}
    for i, (m, w) in enumerate(zip(models, weights)):
        f = model_family(m)
        fam_weight[f] = fam_weight.get(f, 0.0) + w
        members.setdefault(f, []).append(i)
    families = sorted(fam_weight, key=lambda f: -fam_weight[f])      # sorted は安定。同額なら出現順
    colors: list = [None] * len(models)
    order: list[int] = []
    for hue, f in enumerate(families):
        for tone, i in enumerate(members[f]):
            order.append(i)
            if hue < MAX_FAMILIES and tone < MAX_TONES:
                colors[i] = [hue, tone]
    return order, colors


def build_payload(rep: Report) -> dict:
    models_order = list(rep.models.keys())                       # 金額の大きい順
    order, model_color = assign_model_colors(models_order, [b.cost for b in rep.models.values()])
    model_id = {m: i for i, m in enumerate(models_order)}
    projects = list(rep.projects.keys())
    proj_id = {p: i for i, p in enumerate(projects)}
    days = sorted({f.day for f in rep.facts})
    day_id = {d: i for i, d in enumerate(days)}

    turns = []
    for t in rep.turns:
        turns.append([t.start.strftime("%Y-%m-%d %H:%M"), proj_id[t.project], t.session[:8], t.no, t.prompt,
                      None if t.duration is None else round(t.duration, 1)])
    # facts の turn は Report.turns の idx（欠番あり）なので、HTML 側の連番に振り直す
    remap = {t.idx: i for i, t in enumerate(rep.turns)}
    facts = [[remap[f.turn], day_id[f.day], model_id[f.model], f.input, f.output, f.cache_write_5m,
              f.cache_write_1h, f.cache_read, f.calls, _r(f.cost_list), _r(f.cost)] for f in rep.facts]

    model_price = []
    for m in models_order:
        base = m.replace(" (fast)", "")
        p = rep.pricing.resolve(base)
        if p is None:
            model_price.append([0, 0, 0, 0, 0, 1, 0])
        else:
            mult = p.fast_multiplier if m.endswith(" (fast)") else 1.0
            model_price.append([p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read, mult, p.discount_rate])

    warnings = []
    if rep.unpriced_models:
        names = ", ".join(f"{m}({n}回)" for m, n in rep.unpriced_models.items())
        warnings.append(f"単価表に無いモデルがあり、金額を 0 として集計しています: {names}。pricing.csv に追記してください。")
    d = rep.pricing.default_discount
    return {
        "meta": {
            "generated": rep.generated_at.strftime("%Y-%m-%d %H:%M"),
            "tz": rep.tz_label,
            "today": rep.today,
            "first": rep.first.strftime("%Y-%m-%d") if rep.first else "",
            "last": rep.last.strftime("%Y-%m-%d") if rep.last else "",
            "promptChars": rep.prompt_chars,
            "warnings": warnings,
            "notes": [
                "金額はログのトークン数 × 単価表(pricing.csv)による概算です。実際の請求額・Claude Enterprise の契約条件とは異なる場合があります。",
                "ターン = 人間のプロンプト1回から次のプロンプトまで（ツール実行の再呼び出しとサブエージェントを含む）。所要時間 = プロンプト送信から最後の応答まで（ツール許可の待ち時間を含む）。プロジェクト = セッションの作業ディレクトリ(cwd)。",
                f"claude-usage-stats v{__version__} ／ 単価表: {os.path.basename(rep.pricing.source) or '-'}",
            ],
        },
        "models": models_order,
        "modelOrder": order,
        "modelColor": model_color,
        "modelPrice": model_price,
        "projects": [{"path": p, "label": rep.project_labels[p]} for p in projects],
        "days": days,
        "turns": turns,
        "facts": facts,
    }


def render_html(rep: Report) -> str:
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    payload = json.dumps(build_payload(rep), ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")           # </script> による早期終了を防ぐ
    return tpl.replace("__TITLE__", TITLE).replace("__DATA_JSON__", payload)


def write_html(rep: Report, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_html(rep))
    os.replace(tmp, path)
    return path
