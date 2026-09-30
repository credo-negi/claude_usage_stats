"""Claude Code の Stop フック（チャット 1 往復ごとに自動でレポート更新）の登録・解除・実行。

settings.json への変更は「本ツールのエントリだけ」を追加/置換/削除する。既存の設定キーや
他のフックには触れず、書き換え前に必ずバックアップを作る。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Iterator, Optional

MARKER = "claude_usage.py"
LOCK_STALE_SEC = 120


def settings_path(scope: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    if scope == "project":
        return os.path.join(os.getcwd(), ".claude", "settings.json")
    cfg = (os.environ.get("CLAUDE_CONFIG_DIR") or "").split(",")[0].strip()
    return os.path.join(os.path.expanduser(cfg or "~/.claude"), "settings.json")


def _quote(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if os.name == "nt" else shlex.quote(arg)


def build_command(script: str, extra_args: list[str]) -> str:
    """フックに登録するコマンド行。Windows でも bash 系でも解釈できるよう '/' 区切りにする。"""
    def norm(p: str) -> str:
        return p.replace("\\", "/") if os.name == "nt" else p
    parts = [norm(sys.executable), norm(os.path.abspath(script)), "hook"] + list(extra_args)
    return " ".join(_quote(p) for p in parts)


def _is_ours(hook: dict) -> bool:
    cmd = hook.get("command", "") if isinstance(hook, dict) else ""
    return MARKER in cmd and " hook" in cmd


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path} を JSON として読めません（コメント等が含まれていませんか）: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} の最上位が JSON オブジェクトではありません")
    return data


def _save(path: str, data: dict) -> Optional[str]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}"
        n = 1
        while os.path.exists(backup):  # 同じ秒に再実行しても、元のバックアップを上書きしない
            n += 1
            backup = f"{path}.bak.{time.strftime('%Y%m%d%H%M%S')}-{n}"
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return backup


def install(path: str, command: str, timeout: int = 60, dry_run: bool = False) -> tuple[str, Optional[str]]:
    """(動作の説明, バックアップパス)。"""
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings.json の hooks がオブジェクトではありません")
    stop = hooks.setdefault("Stop", [])
    entry = {"type": "command", "command": command, "timeout": timeout}
    action = "追加"
    for group in stop:
        for i, h in enumerate(group.get("hooks", [])):
            if _is_ours(h):
                group["hooks"][i] = entry
                action = "更新"
                break
        else:
            continue
        break
    else:
        stop.append({"hooks": [entry]})
    if dry_run:
        return f"[dry-run] Stop フックを{action}する予定: {command}", None
    return f"Stop フックを{action}しました: {command}", _save(path, data)


def uninstall(path: str, dry_run: bool = False) -> tuple[int, Optional[str]]:
    """(削除した件数, バックアップパス)"""
    data = _load(path)
    stop = (data.get("hooks") or {}).get("Stop")
    removed = 0
    if isinstance(stop, list):
        for group in stop:
            keep = [h for h in group.get("hooks", []) if not _is_ours(h)]
            removed += len(group.get("hooks", [])) - len(keep)
            group["hooks"] = keep
        data["hooks"]["Stop"] = [g for g in stop if g.get("hooks")]
        if not data["hooks"]["Stop"]:
            del data["hooks"]["Stop"]
        if not data["hooks"]:
            del data["hooks"]
    if removed and not dry_run:
        return removed, _save(path, data)
    return removed, None


@contextmanager
def single_instance(lock_path: str) -> Iterator[bool]:
    """フックが連続発火しても二重実行しない簡易ロック。取得できなければ False を返す。"""
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    acquired = False
    try:
        try:
            if os.path.exists(lock_path) and time.time() - os.path.getmtime(lock_path) > LOCK_STALE_SEC:
                os.remove(lock_path)  # 異常終了で残った古いロック
        except OSError:
            pass
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
        except FileExistsError:
            pass
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lock_path)
            except OSError:
                pass
