"""Claude Code のトランスクリプト(JSONL)の探索と解析。

ccusage と同様に ``<config>/projects/**/*.jsonl`` を読み、assistant メッセージの
``message.usage`` からトークン数を取り出す。ccusage との対応:

* 重複排除キーは ``message.id`` + ``requestId``（ストリーミングで同一応答が複数行に
  書かれる／セッション再開でログが複製されるため）。両方そろわない行は重複排除しない。
* モデル名 ``<synthetic>``（API エラー等の疑似応答）は無視する。
* 設定ディレクトリは ``CLAUDE_CONFIG_DIR``（カンマ区切り可）→
  ``$XDG_CONFIG_HOME/claude`` / ``~/.config/claude`` と ``~/.claude`` の順に探す。

ccusage との違い: 「チャット1往復（ターン）」単位で集計するため、人間が入力した
プロンプトも抽出する（``extract_prompt``）。トランスクリプトの仕様は非公開なので、
依存している前提はこのファイルの関数 docstring に集約してある。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class Call:
    """API 呼び出し 1 回分の消費量（重複排除済みの 1 行）。"""
    key: str
    session: str
    ts: str            # UTC ISO-8601 (例 2026-09-21T03:27:19.352Z)
    model: str
    inp: int
    out: int
    cw5: int           # 5 分キャッシュ書き込み
    cw1: int           # 1 時間キャッシュ書き込み
    cr: int            # キャッシュ読み込み
    speed: str         # "standard" | "fast" | ""
    cwd: str
    side: int          # 1 = サブエージェント
    pdir: str          # projects 直下のフォルダ名（cwd が無いときの代替）


@dataclass
class Prompt:
    """人間が入力したプロンプト = 1 往復（ターン）の開始点。"""
    uuid: str
    session: str
    ts: str
    cwd: str
    preview: str


# --------------------------------------------------------------------------
# 探索
# --------------------------------------------------------------------------
def default_claude_dirs(env: Optional[dict] = None) -> list[str]:
    """ログの親ディレクトリ候補（``projects`` を含むもの）を返す。"""
    env = os.environ if env is None else env
    home = os.path.expanduser("~")
    candidates: list[str] = []
    override = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if override:
        candidates += [p.strip() for p in override.split(",") if p.strip()]
    else:
        xdg = (env.get("XDG_CONFIG_HOME") or "").strip() or os.path.join(home, ".config")
        candidates += [os.path.join(xdg, "claude"), os.path.join(home, ".claude")]
    seen, result = set(), []
    for c in candidates:
        c = os.path.abspath(os.path.expanduser(c))
        real = os.path.normcase(os.path.realpath(c))
        if real in seen or not os.path.isdir(os.path.join(c, "projects")):
            continue
        seen.add(real)
        result.append(c)
    return result


def find_transcripts(claude_dirs: Iterable[str]) -> Iterator[tuple[str, str]]:
    """(絶対パス, projects 直下のフォルダ名) を列挙する。サブエージェントのログも含む。"""
    seen: set[str] = set()
    for base in claude_dirs:
        root = os.path.join(base, "projects")
        for dirpath, _dirs, files in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            pdir = "" if rel == "." else rel.split(os.sep)[0]
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                real = os.path.normcase(os.path.realpath(path))
                if real in seen:
                    continue
                seen.add(real)
                yield path, pdir


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
_TAG_BLOCK = re.compile(
    r"<(ide_opened_file|ide_selection|system-reminder|local-command-caveat)>.*?</\1>", re.S)
_CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_NON_PROMPT_PREFIXES = ("<local-command-", "[Request interrupted", "<task-notification",
                        "<system-reminder>")
PREVIEW_MAX = 200


def _clean_preview(text: str) -> str:
    m = _CMD_NAME.search(text)
    if m:  # スラッシュコマンド: "/name args" に整形
        args = _CMD_ARGS.search(text)
        text = m.group(1).strip() + (" " + args.group(1).strip() if args else "")
    text = _TAG_BLOCK.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:PREVIEW_MAX]


def extract_prompt(d: dict) -> Optional[str]:
    """人間が入力したプロンプトなら本文プレビューを返し、そうでなければ None。

    前提（Claude Code のバージョンで変わりうる）:
    * ユーザー入力は ``type == "user"``。ツール結果も同じ type だが content が
      ``tool_result`` ブロックになる。
    * サブエージェント(``isSidechain``)・``isMeta``・圧縮要約(``isCompactSummary``)は対象外。
    * 新しいログでは ``origin.kind`` があり、``"human"`` 以外（task-notification 等）は対象外。
      無い場合は本文の接頭辞（<local-command-…> 等）で判定する。
    """
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    origin = d.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    stripped = text.lstrip()
    if stripped.startswith(_NON_PROMPT_PREFIXES):
        return None
    return _clean_preview(text)


def _int(v) -> int:
    return v if isinstance(v, int) and v > 0 else 0


def extract_call(d: dict, fallback_session: str, pdir: str, line_key: str) -> Optional[Call]:
    """assistant エントリから Call を作る。usage が無い/対象外なら None。"""
    if d.get("type") != "assistant":
        return None
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    model = msg.get("model")
    ts = d.get("timestamp")
    if not isinstance(usage, dict) or not model or model == SYNTHETIC_MODEL or not ts:
        return None
    inp, out = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
    cr = _int(usage.get("cache_read_input_tokens"))
    total_cw = _int(usage.get("cache_creation_input_tokens"))
    cc = usage.get("cache_creation")
    cw5 = cw1 = 0
    if isinstance(cc, dict):
        cw5 = _int(cc.get("ephemeral_5m_input_tokens"))
        cw1 = _int(cc.get("ephemeral_1h_input_tokens"))
    if cw5 + cw1 < total_cw:  # 内訳が無い/不足 → 差分は 5 分キャッシュ扱い
        cw5 += total_cw - (cw5 + cw1)
    if not (inp or out or cr or cw5 or cw1):
        return None
    mid, rid = msg.get("id"), d.get("requestId")
    key = f"{mid}:{rid}" if mid and rid else f"noid:{line_key}"
    speed = usage.get("speed") if isinstance(usage.get("speed"), str) else ""
    return Call(key=key, session=d.get("sessionId") or fallback_session, ts=ts, model=model,
                inp=inp, out=out, cw5=cw5, cw1=cw1, cr=cr, speed=speed,
                cwd=d.get("cwd") or "", side=1 if d.get("isSidechain") else 0, pdir=pdir)


def parse_file(path: str, pdir: str) -> tuple[list[Call], list[Prompt]]:
    """1 ファイルを解析する。ファイル内の重複行は出力トークンが最大のものを残す。"""
    fallback_session = os.path.splitext(os.path.basename(path))[0]
    calls: dict[str, Call] = {}
    prompts: dict[str, Prompt] = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh):
            # 高速化のための事前フィルタ（assistant の usage 行と user 行だけ JSON 解析する）
            if '"usage"' not in line and '"user"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # 書き込み途中の末尾行など
            if not isinstance(d, dict):
                continue
            call = extract_call(d, fallback_session, pdir, f"{path}:{lineno}")
            if call:
                prev = calls.get(call.key)
                if prev is None or call.out >= prev.out:
                    calls[call.key] = call
                continue
            preview = extract_prompt(d)
            if preview is not None and d.get("timestamp"):
                uid = d.get("uuid") or f"{path}:{lineno}"
                prompts[uid] = Prompt(uid, d.get("sessionId") or fallback_session,
                                      d["timestamp"], d.get("cwd") or "", preview)
    return list(calls.values()), list(prompts.values())
