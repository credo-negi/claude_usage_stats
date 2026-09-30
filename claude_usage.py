#!/usr/bin/env python3
"""Claude Code の利用トークン数・概算金額を、ローカルのログから集計して HTML / xlsx で可視化する。

    python claude_usage.py                       # 集計してレポートを生成（既定）
    python claude_usage.py report --days 30      # 直近 30 日だけ
    python claude_usage.py install-hook          # チャット 1 往復ごとに自動更新（Stop フック）
    python claude_usage.py uninstall-hook

詳しくは README.md を参照。Python 3.9 以上、依存は XlsxWriter のみ（xlsx 出力時）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Optional

if os.name == "nt":  # Windows のコンソール既定(cp932)では一部文字が出力できないため UTF-8 に切り替える
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cusage import __version__, hook as hooklib  # noqa: E402
from cusage.aggregate import Filters, Report, Turn, build_report, parse_ts, resolve_tz  # noqa: E402
from cusage.logs import Call, Prompt, default_claude_dirs  # noqa: E402
from cusage.pricing import Pricing  # noqa: E402
from cusage.store import Store, sync  # noqa: E402

COMMANDS = ("report", "hook", "install-hook", "uninstall-hook")
BASENAME = "claude-usage-report"


def data_home() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_USAGE_HOME") or "~/.claude-usage-stats"))


# --------------------------------------------------------------------------
def add_report_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("入力")
    g.add_argument("--claude-dir", action="append", metavar="DIR",
                   help="Claude Code の設定ディレクトリ（<DIR>/projects を読む）。複数指定可。"
                        "省略時は CLAUDE_CONFIG_DIR → ~/.config/claude と ~/.claude")
    g.add_argument("--no-sync", action="store_true", help="ログを再走査せず、蓄積済みキャッシュだけで集計する")
    g.add_argument("--rebuild", action="store_true",
                   help="キャッシュを破棄して全ログを再解析する（元ログが削除済みの履歴は失われます）")
    g = p.add_argument_group("絞り込み")
    g.add_argument("--since", metavar="YYYY-MM-DD", help="この日以降（集計タイムゾーンの日付）")
    g.add_argument("--until", metavar="YYYY-MM-DD", help="この日まで")
    g.add_argument("--days", type=int, metavar="N", help="直近 N 日（今日を含む）。--since より優先")
    g.add_argument("--project", metavar="TEXT", help="作業ディレクトリのパスに TEXT を含むものだけ（大文字小文字無視）")
    g.add_argument("--model", metavar="TEXT", help="モデル ID に TEXT を含むものだけ")
    g = p.add_argument_group("金額")
    g.add_argument("--pricing", metavar="FILE", help="単価表 CSV（既定: 同梱の pricing.csv / CLAUDE_USAGE_PRICING）")
    g.add_argument("--discount", type=float, metavar="RATE",
                   help="契約割引率を小数で（15%% 引き = 0.15）。単価表の割引指定より優先")
    g = p.add_argument_group("出力")
    g.add_argument("--out-dir", metavar="DIR", help="出力先ディレクトリ（既定: <data-home>/reports）")
    g.add_argument("--formats", default="html,xlsx", help="出力形式 html,xlsx（既定: 両方）。'none' で画面表示のみ")
    g.add_argument("--timezone", metavar="TZ", help="集計タイムゾーン（例 Asia/Tokyo, UTC）。既定: OS のローカル")
    g.add_argument("--prompt-chars", type=int, default=0, metavar="N",
                   help="ターン明細に載せるプロンプト先頭の文字数（既定 0 = 載せない。共有するレポートでは 0 推奨）")
    g.add_argument("--no-turn-summary", action="store_true",
                   help="hook: チャット 1 往復ごとのトークン数・概算金額の表示をやめる（レポート更新のみ行う）")
    g.add_argument("-q", "--quiet", action="store_true", help="コンソール出力を出さない")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude_usage.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"claude-usage-stats {__version__}")
    sub = p.add_subparsers(dest="command", metavar="{report,hook,install-hook,uninstall-hook}")
    add_report_args(sub.add_parser("report", help="集計してレポートを生成（既定）"))
    add_report_args(sub.add_parser("hook", help="Stop フックから呼ばれる静かな更新（通常は直接実行しない）"))
    ih = sub.add_parser("install-hook", help="Claude Code の Stop フックに登録する",
                        description="以降の未知オプション(--formats html 等)はそのままフックのコマンドに引き継ぎます。")
    ih.add_argument("--scope", choices=["user", "project"], default="user",
                    help="user: ~/.claude/settings.json / project: ./.claude/settings.json")
    ih.add_argument("--settings", metavar="FILE", help="対象の settings.json を直接指定")
    ih.add_argument("--dry-run", action="store_true", help="変更内容を表示するだけで書き込まない")
    uh = sub.add_parser("uninstall-hook", help="Stop フックの登録を解除する")
    uh.add_argument("--scope", choices=["user", "project"], default="user")
    uh.add_argument("--settings", metavar="FILE")
    uh.add_argument("--dry-run", action="store_true")
    return p


# --------------------------------------------------------------------------
def _date(s: str, opt: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise SystemExit(f"{opt} は YYYY-MM-DD 形式で指定してください: {s!r}")


def make_filters(args) -> Filters:
    f = Filters(project=args.project, model=args.model)
    if args.since:
        f.since = _date(args.since, "--since")
    if args.until:
        f.until = _date(args.until, "--until")
    if args.days:
        if args.days < 1:
            raise SystemExit("--days は 1 以上を指定してください")
        tz, _ = resolve_tz(args.timezone)
        today = (datetime.now(timezone.utc).astimezone(tz) if tz else datetime.now().astimezone()).date()
        f.since = today - timedelta(days=args.days - 1)
    return f


def check_args(args) -> None:
    """レポート系オプションの妥当性検査。不正なら ValueError（フックに不正な設定を登録しないためにも使う）。"""
    formats = {x.strip().lower() for x in args.formats.split(",") if x.strip()} - {"none"}
    if formats - {"html", "xlsx"}:
        raise ValueError(f"--formats は html / xlsx / none のみ指定できます: {', '.join(sorted(formats - {'html', 'xlsx'}))}")
    resolve_tz(args.timezone)
    Pricing.load(args.pricing, args.discount)
    make_filters(args)


def load_data(args, log=lambda _m: None) -> tuple[list[Call], list[Prompt], Pricing, list[str]]:
    """ログを同期して蓄積データを読み出す。(calls, prompts, pricing, ログの場所)"""
    home = data_home()
    dirs = [os.path.abspath(os.path.expanduser(d)) for d in args.claude_dir] if args.claude_dir else default_claude_dirs()
    pricing = Pricing.load(args.pricing, args.discount)
    store = Store(os.path.join(home, "usage.db"))
    try:
        if args.rebuild:
            store.clear()
        if not args.no_sync:
            if not dirs and not store.load_calls():
                raise SystemExit("Claude Code のログが見つかりません（~/.claude/projects）。--claude-dir で場所を指定してください。")
            st = sync(store, dirs, log)
            for e in st.errors:
                log(f"警告: 読み込めないファイル: {e}")
        calls, prompts = store.load_calls(), store.load_prompts()
    finally:
        store.close()
    return calls, prompts, pricing, dirs


def generate(args, log=lambda _m: None, data=None) -> tuple[Report, list[str]]:
    """data: load_data の結果（すでに読み込み済みなら再利用する）"""
    calls, prompts, pricing, dirs = data or load_data(args, log)
    rep = build_report(calls, prompts, pricing, args.timezone, make_filters(args),
                       max(0, args.prompt_chars), sources=dirs)
    home = data_home()
    out_dir = os.path.abspath(os.path.expanduser(args.out_dir)) if args.out_dir else os.path.join(home, "reports")
    written = []
    formats = {x.strip().lower() for x in args.formats.split(",") if x.strip()} - {"none"}
    if formats - {"html", "xlsx"}:
        raise ValueError(f"--formats は html / xlsx / none のみ指定できます: {', '.join(sorted(formats - {'html', 'xlsx'}))}")
    if "html" in formats:
        from cusage.report_html import write_html
        written.append(write_html(rep, os.path.join(out_dir, BASENAME + ".html")))
    if "xlsx" in formats:
        try:
            from cusage.report_xlsx import write_xlsx
        except ImportError:
            raise SystemExit("xlsx の出力には XlsxWriter が必要です: pip install XlsxWriter")
        try:
            written.append(write_xlsx(rep, os.path.join(out_dir, BASENAME + ".xlsx")))
        except PermissionError:
            raise ValueError("xlsx を書き込めません。Excel で開いていませんか？ 閉じてから再実行してください。")
    return rep, written


# --------------------------------------------------------------------------
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _table(headers: list[str], rows: list[list[str]], left: int = 1) -> str:
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(_w(c) for c in col) for col in cols]

    def line(cells):
        out = []
        for i, c in enumerate(cells):
            pad = " " * (widths[i] - _w(c))
            out.append(c + pad if i < left else pad + c)
        return "  ".join(out)
    return "\n".join([line(headers), "  ".join("-" * w for w in widths)] + [line(r) for r in rows])


def _tok(n: int) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


def _dur(sec: float) -> str:
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}時間{m:02d}分" if h else f"{m}分{s:02d}秒" if m else f"{s}秒"


def latest_turn(calls: list[Call], prompts: list[Prompt], pricing: Pricing, session: str,
                tz_name: Optional[str]) -> Optional[Report]:
    """指定セッションの最新ターンだけを含むレポート（絞り込みなし）。無ければ None。"""
    rep = build_report([c for c in calls if c.session == session],
                       [p for p in prompts if p.session == session], pricing, tz_name)
    if not rep.turns:
        return None
    last = max(rep.turns, key=lambda t: t.start)
    rep.turns = [last]
    return rep


def month_cost(calls: list[Call], pricing: Pricing, tz_name: Optional[str],
               now: Optional[datetime] = None) -> tuple[str, float, float]:
    """(当月 'YYYY-MM', 概算金額, 公開価格ベース)。全セッション・全プロジェクトの合計（絞り込みなし）。"""
    tz, _ = resolve_tz(tz_name)

    def local(dt: datetime) -> datetime:
        return dt.astimezone(tz) if tz is not None else dt.astimezone()
    month = local(now or datetime.now(timezone.utc)).strftime("%Y-%m")
    net = listed = 0.0
    for c in calls:
        if local(parse_ts(c.ts)).strftime("%Y-%m") == month:
            l, n = pricing.cost(c)
            listed += l
            net += n
    return month, net, listed


def format_turn_summary(turn: Turn, rep: Report, month: Optional[tuple[str, float, float]] = None) -> str:
    """チャット 1 往復分の使用量と概算金額を 1 行にまとめる（Claude Code の画面に出す文言）。"""
    b = turn.bucket
    parts = [f"トークン {_tok(b.tokens)}"
             f"（入力 {_tok(b.input)} / 出力 {_tok(b.output)} / キャッシュ書込 {_tok(b.cache_write)} / 読込 {_tok(b.cache_read)}）",
             f"概算 ${b.cost:,.2f}" + (f"（公開価格 ${b.cost_list:,.2f}）" if b.discount > 0 else ""),
             f"API {b.calls:,} 回"]
    if turn.duration is not None:
        parts.append(f"所要 {_dur(turn.duration)}")
    text = "今回のターン: " + " ・ ".join(parts)
    if month:
        label, net, listed = month
        text += (f"\n当月（{label}）の概算合計: ${net:,.2f}"
                 + (f"（公開価格 ${listed:,.2f}）" if listed - net > 1e-9 else ""))
    if rep.unpriced_models:
        text += f"\n※単価表に無いモデルは $0 で計算: {', '.join(rep.unpriced_models)}"
    return text


def print_summary(rep: Report, written: list[str]) -> None:
    t = rep.total
    if not rep.first:
        print("該当するデータがありませんでした。")
        return
    print(f"期間: {rep.first:%Y-%m-%d} 〜 {rep.last:%Y-%m-%d} ({rep.tz_label})")
    print(f"ターン {t.turns:,} / API呼出 {t.calls:,} / トークン {_tok(t.tokens)} "
          f"(入力 {_tok(t.input)} 出力 {_tok(t.output)} キャッシュ書込 {_tok(t.cache_write)} 読込 {_tok(t.cache_read)})")
    dur_total, dur_n = rep.duration_stats()
    if dur_n:
        print(f"所要時間: 合計 {_dur(dur_total)} / 1ターン平均 {_dur(dur_total / dur_n)}（{dur_n:,} ターン）")
    if t.discount > 0:
        print(f"概算金額: ${t.cost:,.2f}（公開価格 ${t.cost_list:,.2f} − 契約割引 ${t.discount:,.2f}）\n")
    else:
        print(f"概算金額: ${t.cost:,.2f}（公開価格ベース・契約割引なし）\n")
    head = ["", "ターン", "呼出", "トークン", "金額(USD)"]

    def rows(items, label=lambda k: k, limit=None):
        items = list(items)[: limit or None]
        return [[label(k), f"{b.turns:,}", f"{b.calls:,}", _tok(b.tokens), f"${b.cost:,.2f}"] for k, b in items]
    print("■ 月別\n" + _table(["月"] + head[1:], rows(rep.monthly.items())) + "\n")
    print("■ プロジェクト別（上位10）\n" + _table(["プロジェクト"] + head[1:],
          rows(rep.projects.items(), lambda k: rep.project_labels[k], 10)) + "\n")
    print("■ モデル別\n" + _table(["モデル"] + head[1:], rows(rep.models.items())) + "\n")
    if rep.unpriced_models:
        print("警告: 単価表に無いモデル（金額 0 で集計）: " +
              ", ".join(f"{m}({n}回)" for m, n in rep.unpriced_models.items()) + " → pricing.csv に追記してください\n")
    for w in written:
        print(f"出力: {w}")


# --------------------------------------------------------------------------
def cmd_report(args) -> int:
    log = (lambda _m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    try:
        rep, written = generate(args, log)
    except (ValueError, PermissionError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    if not args.quiet:
        print_summary(rep, written)
    return 0


def _log_failure(home: str) -> None:
    try:
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "hook.log"), "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def cmd_hook(args) -> int:
    """Stop フック: 何があってもセッションを妨げない（常に 0 で終了し、失敗は hook.log に残す）。

    1. ログを同期し、今終わったターン（フック入力の session_id の最新ターン）の使用量と概算金額を
       JSON の systemMessage として出力する（Claude Code の画面に表示される）。
    2. レポート（HTML / xlsx）を更新する。表示を先にするので、レポート生成が失敗しても表示される。
    """
    home = data_home()
    session = hooklib.read_input().get("session_id")
    try:
        with hooklib.single_instance(os.path.join(home, ".hook.lock")) as ok:
            if ok:
                args.quiet = True
                data = load_data(args)
                if not args.no_turn_summary and isinstance(session, str) and session:
                    try:
                        rep = latest_turn(data[0], data[1], data[2], session, args.timezone)
                        if rep:
                            print(json.dumps({"systemMessage": format_turn_summary(
                                rep.turns[0], rep, month_cost(data[0], data[2], args.timezone))},
                                             ensure_ascii=False), flush=True)
                    except Exception:
                        _log_failure(home)
                generate(args, data=data)
    except BaseException:
        _log_failure(home)
    return 0


def cmd_install(args, extra: list[str]) -> int:
    path = hooklib.settings_path(args.scope, args.settings)
    cmd = hooklib.build_command(os.path.abspath(__file__), extra)
    try:
        msg, backup = hooklib.install(path, cmd, dry_run=args.dry_run)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"{path}\n{msg}")
    if backup:
        print(f"バックアップ: {backup}")
    if not args.dry_run:
        print("Claude Code を再起動するか /hooks を一度開くと反映されます。")
    return 0


def cmd_uninstall(args) -> int:
    path = hooklib.settings_path(args.scope, args.settings)
    try:
        n, backup = hooklib.uninstall(path, dry_run=args.dry_run)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(f"{path}\n{n} 件のフックを{'削除予定' if args.dry_run else '削除しました'}。")
    if backup:
        print(f"バックアップ: {backup}")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "report")
    parser = build_parser()
    if argv[0] == "install-hook":
        args, extra = parser.parse_known_args(argv)
        # 引き継ぐオプションが正しいか検証してから登録する
        check = build_parser()
        try:
            check_args(check.parse_args(["report"] + extra))
        except SystemExit:
            return 2
        except (ValueError, OSError) as e:
            print(f"エラー: フックに引き継ぐオプションが不正です: {e}", file=sys.stderr)
            return 1
        return cmd_install(args, extra)
    args = parser.parse_args(argv)
    if args.command == "hook":
        return cmd_hook(args)
    if args.command == "uninstall-hook":
        return cmd_uninstall(args)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
