"""HTML レポート生成。外部 CDN・ライブラリ不要の単体ファイル（社内ネットワーク・オフラインで開ける）。

集計済みの数値ではなく「(ターン, 日, モデル)」単位の事実表を JSON で埋め込み、ブラウザ側で
期間・プロジェクト・モデルの絞り込みに応じて再集計する。金額は Python 側で計算済みの値を
そのまま合算するだけなので、単価ロジックは Python 1 箇所に集約される。
"""
from __future__ import annotations

import json
import os

from . import __version__
from .aggregate import Report

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "report.html")
TITLE = "Claude 利用状況レポート"


def _r(v: float) -> float:
    return round(v, 6)


def build_payload(rep: Report) -> dict:
    models_order = list(rep.models.keys())                       # 金額の大きい順
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
        "modelOrder": list(range(len(models_order))),
        "modelSlot": list(range(len(models_order))),
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
