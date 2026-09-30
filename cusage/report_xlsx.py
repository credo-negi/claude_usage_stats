"""xlsx レポート生成（XlsxWriter）。シート構成:

サマリー / 日次 / 月次 / プロジェクト別 / モデル別 / クロス集計 / ターン明細 / 単価表

集計シートは Excel テーブル（フィルタ・並べ替え可）で、日次・月次・プロジェクト・モデルには
Excel ネイティブのグラフを付ける。値はすべて Python 側で計算済みの数値（式ではない）。
"""
from __future__ import annotations

import os
from typing import Callable

import xlsxwriter

from . import __version__
from .aggregate import Bucket, Report

TOKEN_HEADERS = ["入力", "出力", "キャッシュ書込(5分)", "キャッシュ書込(1時間)", "キャッシュ読込", "合計トークン"]
COST_HEADERS = ["定価ベース(USD)", "割引額(USD)", "概算金額(USD)"]
INK, PAPER = "#0b0b0b", "#ffffff"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def _bucket_cells(b: Bucket) -> list:
    return [b.turns, b.calls, b.input, b.output, b.cache_write_5m, b.cache_write_1h, b.cache_read,
            b.tokens, b.cost_list, b.discount, b.cost]


def write_xlsx(rep: Report, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    wb = xlsxwriter.Workbook(tmp, {"default_date_format": "yyyy-mm-dd hh:mm"})
    wb.set_properties({"title": "Claude 利用状況レポート", "comments": f"claude-usage-stats v{__version__}"})
    F = {
        "title": wb.add_format({"bold": True, "font_size": 16}),
        "h": wb.add_format({"bold": True, "bg_color": "#f0efec", "border": 1, "border_color": "#c3c2b7", "text_wrap": True, "valign": "top"}),
        "int": wb.add_format({"num_format": "#,##0"}),
        "usd": wb.add_format({"num_format": "$#,##0.00"}),
        "usd4": wb.add_format({"num_format": "$#,##0.0000"}),
        "pct": wb.add_format({"num_format": "0.0%"}),
        "bold": wb.add_format({"bold": True}),
        "note": wb.add_format({"font_color": "#52514e", "text_wrap": True, "valign": "top"}),
        "warn": wb.add_format({"bg_color": "#fff4d6", "font_color": "#5c4200", "text_wrap": True}),
        "big": wb.add_format({"bold": True, "font_size": 22, "num_format": "$#,##0.00"}),
        "text": wb.add_format({}),
    }
    n_tok = len(TOKEN_HEADERS)

    def stat_table(ws, first_col_header: str, rows: list[tuple[str, Bucket]], extra_head: list[str] = None,
                   extra_vals: Callable = None, start_row: int = 0, name: str = "") -> tuple[int, int]:
        """(最終データ行, 最終列) を返す。ヘッダは start_row。"""
        # 合計行は Excel テーブルの total_row（SUBTOTAL）で出す。フィルタ中は表示行だけの合計になる
        heads = [first_col_header] + (extra_head or []) + ["ターン数", "API呼出数"] + TOKEN_HEADERS + COST_HEADERS
        ncol_extra = len(extra_head or [])
        data = []
        for key, b in rows:
            data.append([key] + (extra_vals(key, b) if extra_vals else []) + _bucket_cells(b))
        cols = []
        for i, h in enumerate(heads):
            col = {"header": h}
            if i == 0:
                col["total_string"] = "合計"
            elif i > ncol_extra:
                fmt_key = "usd" if h in COST_HEADERS else "int"
                col["format"] = F[fmt_key]
                col["total_function"] = "sum"
            cols.append(col)
        last_row = start_row + len(data) + 1
        ws.add_table(start_row, 0, last_row, len(heads) - 1, {
            "data": data, "columns": cols, "total_row": True, "style": "Table Style Light 1",
            "name": name or None})
        ws.set_column(0, 0, 22)
        if ncol_extra:
            ws.set_column(1, ncol_extra, 34)
        ws.set_column(ncol_extra + 1, len(heads) - 1, 14)
        ws.set_row(start_row, 32)
        return start_row + len(data), len(heads) - 1

    # ---- サマリー ----------------------------------------------------------
    ws = wb.add_worksheet("サマリー")
    ws.hide_gridlines(2)
    ws.set_column(0, 0, 26)
    ws.set_column(1, 1, 22)
    ws.set_column(2, 5, 18)
    ws.write(0, 0, "Claude 利用状況レポート", F["title"])
    period = f"{rep.first:%Y-%m-%d} 〜 {rep.last:%Y-%m-%d}" if rep.first else "データなし"
    d = rep.pricing.default_discount
    info = [
        ("集計期間", period), ("集計タイムゾーン", rep.tz_label), ("生成日時", f"{rep.generated_at:%Y-%m-%d %H:%M}"),
        ("価格基準", f"契約割引 {d * 100:g}% を適用" if d else "公開価格（割引なし）"),
        ("単価表", os.path.basename(rep.pricing.source) or "-"),
    ]
    r = 2
    for k, v in info:
        ws.write(r, 0, k, F["bold"]); ws.write(r, 1, v); r += 1
    r += 1
    ws.write(r, 0, "概算利用金額(USD)", F["bold"]); ws.write_number(r, 1, rep.total.cost, F["big"]); ws.set_row(r, 32); r += 1
    kpis = [("定価ベース(USD)", rep.total.cost_list, "usd"), ("割引額(USD)", rep.total.discount, "usd"),
            ("ターン数", rep.total.turns, "int"), ("API呼出数", rep.total.calls, "int"),
            ("合計トークン", rep.total.tokens, "int"), ("入力トークン", rep.total.input, "int"),
            ("出力トークン", rep.total.output, "int"),
            ("キャッシュ書込トークン", rep.total.cache_write, "int"), ("キャッシュ読込トークン", rep.total.cache_read, "int"),
            ("1ターン平均(USD)", rep.total.cost / rep.total.turns if rep.total.turns else 0, "usd"),
            ("稼働日あたり(USD)", rep.total.cost / len(rep.daily) if rep.daily else 0, "usd")]
    for k, v, f in kpis:
        ws.write(r, 0, k); ws.write_number(r, 1, v, F[f]); r += 1
    if rep.unpriced_models:
        r += 1
        msg = "単価表に無いモデルは金額 0 で集計されています: " + ", ".join(f"{m}({n}回)" for m, n in rep.unpriced_models.items()) \
              + "。pricing.csv に追記してください。"
        ws.merge_range(r, 0, r, 5, msg, F["warn"]); ws.set_row(r, 32); r += 1
    r += 1
    ws.write(r, 0, "トークン種別の内訳", F["bold"]); r += 1
    for i, h in enumerate(["種別", "トークン数", "概算金額(USD)", "金額構成比"]):
        ws.write(r, i, h, F["h"])
    kind_names = {"input": "入力", "output": "出力", "cache_write_5m": "キャッシュ書込(5分)",
                  "cache_write_1h": "キャッシュ書込(1時間)", "cache_read": "キャッシュ読込"}
    total_kind_cost = sum(c for _t, c in rep.kind_costs.values()) or 1.0
    r += 1
    for kind, (tok, cost) in rep.kind_costs.items():
        ws.write(r, 0, kind_names[kind]); ws.write_number(r, 1, tok, F["int"])
        ws.write_number(r, 2, cost, F["usd"]); ws.write_number(r, 3, cost / total_kind_cost, F["pct"]); r += 1
    r += 1
    ws.merge_range(r, 0, r + 3, 5,
                   "金額はログのトークン数 × 単価表(pricing.csv)による概算で、実際の請求額とは異なる場合があります。\n"
                   "ターン = 人間のプロンプト1回から次のプロンプトまで（ツール実行の再呼び出し・サブエージェントを含む）。\n"
                   "プロジェクト = セッションの作業ディレクトリ(cwd)。Fast モードは別モデル「(fast)」として集計。",
                   F["note"])

    # ---- 日次 ----------------------------------------------------------
    ws = wb.add_worksheet("日次")
    ws.freeze_panes(1, 1)
    last, ncol = stat_table(ws, "日付", list(rep.daily.items()), ["主なモデル"],
                            lambda k, b: [", ".join(b.top_models())], name="Daily")
    cost_col = ncol
    if rep.daily:
        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "概算金額(USD)", "categories": ["日次", 1, 0, last, 0],
                       "values": ["日次", 1, cost_col, last, cost_col], "fill": {"color": SERIES[0]}, "gap": 40})
        ch.set_title({"name": "日次の概算金額(USD)", "name_font": {"size": 12}})
        ch.set_legend({"none": True}); ch.set_y_axis({"num_format": "$#,##0", "major_gridlines": {"visible": True, "line": {"color": "#e1e0d9"}}})
        ch.set_x_axis({"num_font": {"rotation": -45}, "date_axis": False})
        ch.set_size({"width": 900, "height": 320})
        ws.insert_chart(last + 4, 0, ch)

    # ---- 月次 ----------------------------------------------------------
    ws = wb.add_worksheet("月次")
    ws.freeze_panes(1, 1)
    last, ncol = stat_table(ws, "月", list(rep.monthly.items()), ["主なモデル"],
                            lambda k, b: [", ".join(b.top_models())], name="Monthly")
    if rep.monthly:
        ch = wb.add_chart({"type": "column"})
        ch.add_series({"name": "概算金額(USD)", "categories": ["月次", 1, 0, last, 0],
                       "values": ["月次", 1, ncol, last, ncol], "fill": {"color": SERIES[0]}, "gap": 60,
                       "data_labels": {"value": True, "num_format": "$#,##0"}})
        ch.set_title({"name": "月次の概算金額(USD)", "name_font": {"size": 12}})
        ch.set_legend({"none": True}); ch.set_y_axis({"num_format": "$#,##0", "major_gridlines": {"visible": True, "line": {"color": "#e1e0d9"}}})
        ch.set_size({"width": 720, "height": 320})
        ws.insert_chart(last + 4, 0, ch)

    # ---- プロジェクト別 ----------------------------------------------------
    ws = wb.add_worksheet("プロジェクト別")
    ws.freeze_panes(1, 1)
    prows = [(rep.project_labels[p], b) for p, b in rep.projects.items()]
    paths = {rep.project_labels[p]: p for p in rep.projects}
    last, ncol = stat_table(ws, "プロジェクト", prows, ["ディレクトリ"], lambda k, b: [paths[k]], name="Projects")
    if prows:
        n = min(15, len(prows))
        ch = wb.add_chart({"type": "bar"})
        ch.add_series({"name": "概算金額(USD)", "categories": ["プロジェクト別", 1, 0, n, 0],
                       "values": ["プロジェクト別", 1, ncol, n, ncol], "fill": {"color": SERIES[0]}, "gap": 50,
                       "data_labels": {"value": True, "num_format": "$#,##0.00"}})
        ch.set_title({"name": f"プロジェクト別の概算金額(USD) 上位{n}件", "name_font": {"size": 12}})
        ch.set_legend({"none": True}); ch.set_y_axis({"reverse": True})
        ch.set_x_axis({"num_format": "$#,##0", "major_gridlines": {"visible": True, "line": {"color": "#e1e0d9"}}})
        ch.set_size({"width": 820, "height": 60 + 26 * n})
        ws.insert_chart(last + 4, 0, ch)

    # ---- モデル別 ----------------------------------------------------------
    ws = wb.add_worksheet("モデル別")
    ws.freeze_panes(1, 1)
    last, ncol = stat_table(ws, "モデル", list(rep.models.items()), name="Models")
    if rep.models:
        ch = wb.add_chart({"type": "pie"})
        n = min(8, len(rep.models))
        ch.add_series({"name": "概算金額", "categories": ["モデル別", 1, 0, n, 0], "values": ["モデル別", 1, ncol, n, ncol],
                       "points": [{"fill": {"color": SERIES[i % 8]}} for i in range(n)],
                       "data_labels": {"percentage": True, "leader_lines": True}})
        ch.set_title({"name": "モデル別の概算金額の構成比", "name_font": {"size": 12}})
        ch.set_size({"width": 560, "height": 340})
        ws.insert_chart(last + 4, 0, ch)

    # ---- クロス集計 --------------------------------------------------------
    ws = wb.add_worksheet("クロス集計")
    ws.set_column(0, 0, 30)
    ws.set_column(1, 40, 15)
    row = 0

    def matrix(title: str, data: dict, col_label: Callable[[str], str] = lambda x: x, row_label=lambda x: x) -> None:
        nonlocal row
        ws.write(row, 0, title, F["bold"]); row += 1
        cols = sorted({c for v in data.values() for c in v}, key=lambda c: -sum(v.get(c, 0) for v in data.values()))
        ws.write(row, 0, "", F["h"])
        for j, c in enumerate(cols):
            ws.write(row, 1 + j, col_label(c), F["h"])
        ws.write(row, 1 + len(cols), "合計", F["h"]); ws.set_row(row, 32); row += 1
        first = row
        for k, v in data.items():
            ws.write(row, 0, row_label(k))
            for j, c in enumerate(cols):
                ws.write_number(row, 1 + j, v.get(c, 0.0), F["usd"])
            ws.write_number(row, 1 + len(cols), sum(v.values()), F["usd"]); row += 1
        ws.write(row, 0, "合計", F["bold"])
        for j in range(len(cols) + 1):
            colname = xlsxwriter.utility.xl_col_to_name(1 + j)
            ws.write_formula(row, 1 + j, f"=SUM({colname}{first + 1}:{colname}{row})", F["usd"])
        row += 3

    matrix("月 × プロジェクト（概算金額 USD）", rep.month_project, col_label=lambda p: rep.project_labels.get(p, p))
    matrix("月 × モデル（概算金額 USD）", rep.month_model)
    matrix("日 × モデル（概算金額 USD）", rep.day_model)

    # ---- ターン明細 --------------------------------------------------------
    ws = wb.add_worksheet("ターン明細")
    heads = ["開始日時", "日付", "プロジェクト", "ディレクトリ", "セッションID", "ターン番号", "API呼出数", "うちサブエージェント",
             "モデル"] + TOKEN_HEADERS + COST_HEADERS
    if rep.prompt_chars:
        heads.append("プロンプト(先頭)")
    data = []
    for t in rep.turns:
        b = t.bucket
        row_ = [t.start.replace(tzinfo=None), t.start.strftime("%Y-%m-%d"), rep.project_labels[t.project], t.project,
                t.session, t.no, b.calls, b.sub_calls, ", ".join(b.top_models(5)),
                b.input, b.output, b.cache_write_5m, b.cache_write_1h, b.cache_read, b.tokens,
                b.cost_list, b.discount, b.cost]
        if rep.prompt_chars:
            row_.append(t.prompt)
        data.append(row_)
    date_fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm"})
    cols = []
    for i, h in enumerate(heads):
        c = {"header": h}
        if i == 0:
            c["format"] = date_fmt
        elif 9 <= i <= 14 or i in (5, 6, 7):
            c["format"] = F["int"]
        elif 15 <= i <= 17:
            c["format"] = F["usd4"]
        cols.append(c)
    last_row = len(data) + 1
    ws.add_table(0, 0, last_row, len(heads) - 1, {"data": data, "columns": cols, "style": "Table Style Light 1",
                                                   "name": "Turns", "total_row": False})
    ws.freeze_panes(1, 1)
    ws.set_row(0, 32)
    ws.set_column(0, 0, 17); ws.set_column(1, 1, 11); ws.set_column(2, 2, 22); ws.set_column(3, 3, 36)
    ws.set_column(4, 4, 38); ws.set_column(5, 7, 11); ws.set_column(8, 8, 28); ws.set_column(9, 17, 15)
    if rep.prompt_chars:
        ws.set_column(18, 18, 60)

    # ---- 単価表 ------------------------------------------------------------
    ws = wb.add_worksheet("単価表")
    heads = ["モデル(前方一致キー)", "入力", "出力", "キャッシュ書込(5分)", "キャッシュ書込(1時間)", "キャッシュ読込",
             "Fast倍率", "割引率", "備考"]
    ws.write(0, 0, f"単価(USD/100万トークン・定価) ／ 出所: {rep.pricing.source or '-'}", F["bold"])
    for i, h in enumerate(heads):
        ws.write(2, i, h, F["h"])
    for i, p in enumerate(rep.pricing.models.values()):
        vals = [p.key, p.input, p.output, p.cache_write_5m, p.cache_write_1h, p.cache_read, p.fast_multiplier, p.discount_rate, p.note]
        for j, v in enumerate(vals):
            if j == 0 or j == 8:
                ws.write(3 + i, j, v)
            elif j == 7:
                ws.write_number(3 + i, j, v, F["pct"])
            elif j == 6:
                ws.write_number(3 + i, j, v)
            else:
                ws.write_number(3 + i, j, v, F["usd"])
    ws.set_column(0, 0, 26); ws.set_column(1, 7, 16); ws.set_column(8, 8, 40)
    ws.set_row(2, 32)

    wb.close()
    os.replace(tmp, path)
    return path
