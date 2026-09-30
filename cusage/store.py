"""解析結果の永続キャッシュ（SQLite・標準ライブラリのみ）。

役割は 2 つ:
1. 増分更新: ファイルのサイズ/更新時刻が変わったものだけ再解析する（Stop フックで毎ターン
   実行しても軽くする）。
2. 履歴の保持: Claude Code の設定(cleanupPeriodDays 等)や運用で古いトランスクリプトが削除される
   ことがある。元ログが消えても、ここに残した記録から月次・年次の集計を続けられる。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .logs import Call, Prompt, find_transcripts, parse_file

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
  key TEXT PRIMARY KEY, session TEXT NOT NULL, ts TEXT NOT NULL, model TEXT NOT NULL,
  inp INTEGER, out INTEGER, cw5 INTEGER, cw1 INTEGER, cr INTEGER,
  speed TEXT, cwd TEXT, side INTEGER, pdir TEXT);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE TABLE IF NOT EXISTS prompts (
  uuid TEXT PRIMARY KEY, session TEXT NOT NULL, ts TEXT NOT NULL, cwd TEXT, preview TEXT);
"""


@dataclass
class SyncStats:
    files_seen: int = 0
    files_parsed: int = 0
    calls_in_parsed_files: int = 0
    errors: list[str] = field(default_factory=list)


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA busy_timeout=30000")
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.db.executescript(_SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # -- 書き込み ----------------------------------------------------------
    def save_file(self, path: str, size: int, mtime_ns: int,
                  calls: Iterable[Call], prompts: Iterable[Prompt]) -> None:
        with self.db:  # 1 トランザクション
            for c in calls:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c.key, c.session, c.ts, c.model, c.inp, c.out, c.cw5, c.cw1, c.cr,
                     c.speed, c.cwd, c.side, c.pdir))
                if cur.rowcount == 0:  # 既存: 出力トークンが多い方（=完成形）を採用
                    self.db.execute(
                        "UPDATE calls SET inp=?,out=?,cw5=?,cw1=?,cr=?,speed=? "
                        "WHERE key=? AND out<?",
                        (c.inp, c.out, c.cw5, c.cw1, c.cr, c.speed, c.key, c.out))
            for p in prompts:
                self.db.execute("INSERT OR IGNORE INTO prompts VALUES (?,?,?,?,?)",
                                (p.uuid, p.session, p.ts, p.cwd, p.preview))
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (path, size, mtime_ns))

    # -- 読み出し ----------------------------------------------------------
    def file_state(self, path: str) -> Optional[tuple[int, int]]:
        row = self.db.execute("SELECT size, mtime_ns FROM files WHERE path=?", (path,)).fetchone()
        return (row[0], row[1]) if row else None

    def load_calls(self) -> list[Call]:
        rows = self.db.execute(
            "SELECT key,session,ts,model,inp,out,cw5,cw1,cr,speed,cwd,side,pdir FROM calls").fetchall()
        return [Call(*r) for r in rows]

    def load_prompts(self) -> list[Prompt]:
        rows = self.db.execute("SELECT uuid,session,ts,cwd,preview FROM prompts").fetchall()
        return [Prompt(*r) for r in rows]

    def clear(self) -> None:
        with self.db:
            for t in ("files", "calls", "prompts"):
                self.db.execute(f"DELETE FROM {t}")


def sync(store: Store, claude_dirs: Iterable[str],
         log: Callable[[str], None] = lambda _m: None) -> SyncStats:
    """ログを走査し、新規/変更されたファイルだけ解析して DB に反映する。"""
    stats = SyncStats()
    for path, pdir in find_transcripts(claude_dirs):
        stats.files_seen += 1
        try:
            st = os.stat(path)
        except OSError as e:
            stats.errors.append(f"{path}: {e}")
            continue
        if store.file_state(path) == (st.st_size, st.st_mtime_ns):
            continue
        try:
            calls, prompts = parse_file(path, pdir)
        except OSError as e:
            stats.errors.append(f"{path}: {e}")
            continue
        store.save_file(path, st.st_size, st.st_mtime_ns, calls, prompts)
        stats.files_parsed += 1
        stats.calls_in_parsed_files += len(calls)
    log(f"ログ {stats.files_seen} 件を確認、{stats.files_parsed} 件を解析")
    return stats
