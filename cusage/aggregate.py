"""Call/Prompt を「ターン（チャット1往復）」に割り当て、日・月・プロジェクト・モデル別に集計する。

ターンの定義: 人間のプロンプト送信から、次のプロンプトが送信されるまでの間に発生した
全 API 呼び出し（ツール実行ごとの再呼び出し・サブエージェントを含む）。
所要時間 = プロンプト送信から、そのターンの最後の応答（メインスレッドの最後の API 呼び出しの記録時刻）まで。
呼び出しは「同一セッション内で、自分より前(同時刻含む)の最も新しいプロンプト」に割り当てる。
ファイルの並び順に依存しないため、サブエージェントが別ファイルに書かれていても正しく合算される。
"""
from __future__ import annotations

import re
import time
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Optional

from .logs import Call, Prompt
from .pricing import TOKEN_KINDS, Pricing, display_model


@dataclass
class Bucket:
    calls: int = 0
    sub_calls: int = 0
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    cost_list: float = 0.0
    cost: float = 0.0
    turn_ids: set = field(default_factory=set)
    model_cost: dict = field(default_factory=lambda: defaultdict(float))

    def add(self, c: Call, listed: float, net: float, turn_id: int, model: str) -> None:
        self.calls += 1
        self.sub_calls += c.side
        self.input += c.inp
        self.output += c.out
        self.cache_write_5m += c.cw5
        self.cache_write_1h += c.cw1
        self.cache_read += c.cr
        self.cost_list += listed
        self.cost += net
        self.turn_ids.add(turn_id)
        self.model_cost[model] += net

    @property
    def turns(self) -> int:
        return len(self.turn_ids)

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @property
    def discount(self) -> float:
        return self.cost_list - self.cost

    def top_models(self, n: int = 3) -> list[str]:
        return [m for m, _ in sorted(self.model_cost.items(), key=lambda kv: -kv[1])[:n]]


@dataclass
class Turn:
    idx: int
    session: str
    no: int                       # セッション内の通し番号（1 始まり。プロンプト不明の呼び出しは 0）
    start: datetime               # 表示タイムゾーンでの開始時刻
    project: str
    prompt: str
    duration: Optional[float] = None   # 所要時間(秒)。プロンプト不明のターンは None
    bucket: Bucket = field(default_factory=Bucket)


@dataclass
class Fact:
    """HTML のフィルタ再集計用の最小単位: (ターン, 日, モデル)。"""
    turn: int
    day: str
    project: str
    model: str
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    calls: int = 0
    cost_list: float = 0.0
    cost: float = 0.0


@dataclass
class Filters:
    since: Optional[date] = None
    until: Optional[date] = None
    project: Optional[str] = None     # 部分一致（大文字小文字無視）
    model: Optional[str] = None


@dataclass
class Report:
    tz_label: str
    generated_at: datetime
    today: str
    first: Optional[datetime]
    last: Optional[datetime]
    total: Bucket
    daily: dict[str, Bucket]
    monthly: dict[str, Bucket]
    projects: dict[str, Bucket]
    models: dict[str, Bucket]
    turns: list[Turn]
    facts: list[Fact]
    kind_costs: dict[str, tuple[int, float]]     # トークン種別 -> (トークン数, 割引後金額)
    month_project: dict[str, dict[str, float]]
    month_model: dict[str, dict[str, float]]
    day_model: dict[str, dict[str, float]]
    unpriced_models: dict[str, int]              # 単価表に無いモデル -> 呼び出し数
    pricing: Pricing
    prompt_chars: int
    project_labels: dict[str, str]
    sources: list[str] = field(default_factory=list)

    def duration_stats(self) -> tuple[float, int]:
        """(所要時間の合計秒, 所要時間を算出できたターン数)"""
        ds = [t.duration for t in self.turns if t.duration is not None]
        return sum(ds), len(ds)


# --------------------------------------------------------------------------
def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def resolve_tz(name: Optional[str]):
    """(tzinfo または None=OS のローカル, 表示ラベル)"""
    if not name:
        return None, time.strftime("%Z") or "local"
    if name.upper() == "UTC":
        return timezone.utc, "UTC"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception as e:  # ZoneInfoNotFoundError / ImportError
        raise ValueError(
            f"タイムゾーン '{name}' を解決できません（Windows では `pip install tzdata` が必要です）: {e}")


def decode_project_dir(pdir: str) -> str:
    """~/.claude/projects 直下のフォルダ名から元のパスを推定する（'/' も '.' も '-' になる不可逆変換）。"""
    if not pdir:
        return "(不明)"
    if re.match(r"^[A-Za-z]--", pdir):        # Windows: C--Users-foo-proj
        return pdir[0] + ":\\" + pdir[3:].replace("-", "\\")
    return "/" + pdir.lstrip("-").replace("-", "/")


def _basename(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]", path) if p]
    return parts[-1] if parts else path


def make_project_labels(paths: Iterable[str]) -> dict[str, str]:
    """短い表示名を作る。末尾ディレクトリ名が衝突する場合は親ディレクトリも付ける。"""
    paths = sorted(set(paths))
    labels = {p: _basename(p) for p in paths}
    counts = defaultdict(int)
    for v in labels.values():
        counts[v] += 1
    for p in paths:
        if counts[labels[p]] > 1:
            parts = [x for x in re.split(r"[\\/]", p) if x]
            labels[p] = "/".join(parts[-2:]) if len(parts) >= 2 else p
    return labels


def assign_turns(calls: list[Call], prompts: list[Prompt], to_local: Callable[[datetime], datetime]
                 ) -> tuple[list[Turn], dict[str, int]]:
    """(ターン一覧, call.key -> turn idx)。ターンは開始時刻順。"""
    by_session: dict[str, list[tuple[datetime, str, str, str]]] = defaultdict(list)
    for p in prompts:
        by_session[p.session].append((parse_ts(p.ts), p.uuid, p.cwd, p.preview))
    for lst in by_session.values():
        lst.sort()
    prompt_times = {s: [t for t, *_ in lst] for s, lst in by_session.items()}

    # セッションの所属プロジェクト = 最初に現れた cwd（途中で cd しても 1 セッション = 1 プロジェクト）
    first_seen: dict[str, tuple[datetime, str]] = {}
    for c in calls:
        if c.cwd:
            t = parse_ts(c.ts)
            if c.session not in first_seen or t < first_seen[c.session][0]:
                first_seen[c.session] = (t, c.cwd)
    pdir_of = {c.session: c.pdir for c in calls}
    for s, lst in by_session.items():
        for t, _u, cwd, _pv in lst:
            if cwd and (s not in first_seen or t < first_seen[s][0]):
                first_seen[s] = (t, cwd)
    project_of = {s: (first_seen[s][1] if s in first_seen else decode_project_dir(pdir_of.get(s, "")))
                  for s in set(pdir_of) | set(by_session)}

    groups: dict[tuple[str, int], list[Call]] = defaultdict(list)
    for c in calls:
        times = prompt_times.get(c.session, [])
        groups[(c.session, bisect_right(times, parse_ts(c.ts)) - 1)].append(c)

    turns: list[Turn] = []
    for (session, pi), cs in groups.items():
        first_call = min(parse_ts(c.ts) for c in cs)
        if pi >= 0:
            start, _u, _cwd, preview = by_session[session][pi]
        else:
            start, preview = first_call, ""
        # 終了 = メインスレッドの最後の応答。バックグラウンドのサブエージェントが後から動いても延ばさない
        end = max(parse_ts(c.ts) for c in (cs if all(c.side for c in cs) else [c for c in cs if not c.side]))
        duration = max((end - start).total_seconds(), 0.0) if pi >= 0 else None
        turns.append(Turn(idx=-1, session=session, no=pi + 1, start=to_local(min(start, first_call)),
                          project=project_of[session], prompt=preview, duration=duration))
        turns[-1].__dict__["_calls"] = cs
    turns.sort(key=lambda t: (t.start, t.session, t.no))
    key_to_turn: dict[str, int] = {}
    for i, t in enumerate(turns):
        t.idx = i
        for c in t.__dict__.pop("_calls"):
            key_to_turn[c.key] = i
    return turns, key_to_turn


def build_report(calls: list[Call], prompts: list[Prompt], pricing: Pricing,
                 tz_name: Optional[str] = None, filters: Optional[Filters] = None,
                 prompt_chars: int = 0, sources: Optional[list[str]] = None) -> Report:
    filters = filters or Filters()
    tz, tz_label = resolve_tz(tz_name)

    def to_local(dt: datetime) -> datetime:
        return dt.astimezone(tz) if tz is not None else dt.astimezone()

    turns, key_to_turn = assign_turns(calls, prompts, to_local)
    n_all_turns = len(turns)

    total = Bucket()
    daily: dict[str, Bucket] = defaultdict(Bucket)
    monthly: dict[str, Bucket] = defaultdict(Bucket)
    projects: dict[str, Bucket] = defaultdict(Bucket)
    models: dict[str, Bucket] = defaultdict(Bucket)
    kind_tokens = {n: 0 for n, _a, _r in TOKEN_KINDS}
    kind_cost = {n: 0.0 for n, _a, _r in TOKEN_KINDS}
    month_project: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    month_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    day_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    facts: dict[tuple[int, str, str], Fact] = {}
    unpriced: dict[str, int] = defaultdict(int)
    first = last = None

    for c in sorted(calls, key=lambda c: c.ts):
        tidx = key_to_turn[c.key]
        turn = turns[tidx]
        local = to_local(parse_ts(c.ts))
        day = local.strftime("%Y-%m-%d")
        # Fast モードは単価が異なるため別モデルとして扱う（例: claude-opus-5-5 (fast)）
        model = display_model(c.model) + (" (fast)" if c.speed == "fast" else "")
        if filters.since and local.date() < filters.since:
            continue
        if filters.until and local.date() > filters.until:
            continue
        if filters.project and filters.project.lower() not in turn.project.lower():
            continue
        if filters.model and filters.model.lower() not in c.model.lower():
            continue

        listed, net = pricing.cost(c)
        if pricing.resolve(c.model) is None:
            unpriced[display_model(c.model)] += 1
        month = day[:7]
        for b in (total, daily[day], monthly[month], projects[turn.project], models[model], turn.bucket):
            b.add(c, listed, net, tidx, model)
        for name, cost in pricing.token_kind_costs(c).items():
            kind_cost[name] += cost
        for name, attr, _rate in TOKEN_KINDS:
            kind_tokens[name] += getattr(c, attr)
        month_project[month][turn.project] += net
        month_model[month][model] += net
        day_model[day][model] += net
        f = facts.setdefault((tidx, day, model), Fact(tidx, day, turn.project, model))
        f.input += c.inp
        f.output += c.out
        f.cache_write_5m += c.cw5
        f.cache_write_1h += c.cw1
        f.cache_read += c.cr
        f.calls += 1
        f.cost_list += listed
        f.cost += net
        first = local if first is None or local < first else first
        last = local if last is None or local > last else last

    used = {f.turn for f in facts.values()}
    kept_turns = [t for t in turns if t.idx in used]
    now = to_local(datetime.now(timezone.utc))
    if prompt_chars <= 0:
        for t in kept_turns:
            t.prompt = ""
    else:
        for t in kept_turns:
            t.prompt = t.prompt[:prompt_chars]

    return Report(
        tz_label=tz_label, generated_at=now, today=now.strftime("%Y-%m-%d"), first=first, last=last,
        total=total, daily=dict(sorted(daily.items())), monthly=dict(sorted(monthly.items())),
        projects=dict(sorted(projects.items(), key=lambda kv: -kv[1].cost)),
        models=dict(sorted(models.items(), key=lambda kv: -kv[1].cost)),
        turns=kept_turns, facts=sorted(facts.values(), key=lambda f: (f.day, f.turn, f.model)),
        kind_costs={n: (kind_tokens[n], kind_cost[n]) for n in kind_cost},
        month_project={k: dict(v) for k, v in sorted(month_project.items())},
        month_model={k: dict(v) for k, v in sorted(month_model.items())},
        day_model={k: dict(v) for k, v in sorted(day_model.items())},
        unpriced_models=dict(unpriced), pricing=pricing, prompt_chars=prompt_chars,
        project_labels=make_project_labels(projects.keys()), sources=sources or [])
