"""単価表(pricing.csv)の読み込みと金額計算。

ccusage は LiteLLM の価格表をネットワークから取得するが、ここでは同梱の pricing.csv
（Excel で編集可能）を使う。オフライン・社内ネットワークでも動き、契約割引を反映できる。
ログに costUSD が入っていても使わず、常にトークン数から再計算する（新しい Claude Code は
costUSD を出力しないため）。
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_PRICING_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "pricing.csv")
PER = 1_000_000

# トークン種別 → (Call の属性名, 単価キー)。課金対象を増やすときはここに足す。
TOKEN_KINDS = [
    ("input", "inp", "input"),
    ("output", "out", "output"),
    ("cache_write_5m", "cw5", "cache_write_5m"),
    ("cache_write_1h", "cw1", "cache_write_1h"),
    ("cache_read", "cr", "cache_read"),
]


@dataclass(frozen=True)
class ModelPrice:
    key: str
    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    fast_multiplier: float
    discount_rate: float
    note: str = ""


def normalize_model(model: str) -> str:
    """モデル ID を照合用に正規化する（プロバイダ接頭辞・日付・バージョン接尾辞を除去）。"""
    m = model.strip().lower()
    m = re.sub(r"^.*anthropic[./]", "", m)          # anthropic.claude-…, us.anthropic.claude-…
    m = re.sub(r"@.*$", "", m)                       # Vertex: claude-…@20250514
    m = re.sub(r"-v\d+(:\d+)?$", "", m)              # Bedrock: …-v1:0
    m = re.sub(r"-\d{8}$", "", m)                    # 日付サフィックス
    return m


def display_model(model: str) -> str:
    """レポート上のモデル名（日付サフィックスだけ落として同一モデルをまとめる）。"""
    return re.sub(r"-\d{8}$", "", re.sub(r"@.*$", "", model))


def _valid_rate(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1


class Pricing:
    def __init__(self, rows: list[dict], discount_override: Optional[float] = None, source: str = ""):
        """rows: 単価表の各行（dict）。行番号は rows[i]["_line"] があればエラー表示に使う。"""
        self.source = source
        self.currency = "USD"
        default = 0.0
        for r in rows:
            if (r.get("model") or "").strip() == "*":
                default = self._rate(r.get("discount_rate"), r, 0.0)
        self._override = discount_override is not None
        self.default_discount = discount_override if self._override else default
        if not _valid_rate(self.default_discount):
            raise ValueError(f"割引率は 0 以上 1 未満の小数で指定してください（15% 引きなら 0.15）: {self.default_discount!r}")
        self.models: dict[str, ModelPrice] = {}
        for r in rows:
            key = (r.get("model") or "").strip().lower()
            if not key or key == "*":
                continue
            inp = self._num(r, "input_per_mtok", required=True)
            out = self._num(r, "output_per_mtok", required=True)
            # CLI の --discount は表内のモデル別指定より優先する
            disc = self.default_discount if self._override else self._rate(r.get("discount_rate"), r, self.default_discount)
            self.models[key] = ModelPrice(
                key=key, input=inp, output=out,
                cache_write_5m=self._num(r, "cache_write_5m_per_mtok", default=inp * 1.25),
                cache_write_1h=self._num(r, "cache_write_1h_per_mtok", default=inp * 2.0),
                cache_read=self._num(r, "cache_read_per_mtok", default=inp * 0.1),
                fast_multiplier=self._num(r, "fast_multiplier", default=1.0),
                discount_rate=disc, note=(r.get("note") or "").strip())
        self._keys_longest_first = sorted(self.models, key=len, reverse=True)
        self._cache: dict[str, Optional[ModelPrice]] = {}

    @staticmethod
    def _where(row: dict) -> str:
        return f"{row.get('_line', '?')} 行目 ({(row.get('model') or '').strip()})"

    @classmethod
    def _num(cls, row: dict, col: str, default: Optional[float] = None, required: bool = False) -> float:
        raw = (row.get(col) or "").strip()
        if not raw:
            if required:
                raise ValueError(f"単価表 {cls._where(row)}: {col} が空です")
            return float(default)
        try:
            v = float(raw)
        except ValueError:
            raise ValueError(f"単価表 {cls._where(row)}: {col} が数値ではありません: {raw!r}")
        if v < 0:
            raise ValueError(f"単価表 {cls._where(row)}: {col} は 0 以上にしてください: {raw!r}")
        return v

    @classmethod
    def _rate(cls, raw, row: dict, default: float) -> float:
        raw = (raw or "").strip() if isinstance(raw, str) else raw
        if raw in ("", None):
            return default
        try:
            v = float(raw)
        except ValueError:
            v = -1.0
        if not _valid_rate(v):
            raise ValueError(f"単価表 {cls._where(row)}: discount_rate は 0 以上 1 未満の小数で指定してください"
                             f"（15% 引きなら 0.15）: {raw!r}")
        return v

    @classmethod
    def load(cls, path: Optional[str] = None, discount_override: Optional[float] = None) -> "Pricing":
        path = path or os.environ.get("CLAUDE_USAGE_PRICING") or DEFAULT_PRICING_PATH
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:   # Excel の BOM 付き UTF-8 も可
            lines = [(i, ln) for i, ln in enumerate(fh, 1) if ln.strip() and not ln.lstrip().startswith("#")]
        if not lines:
            raise ValueError(f"単価表が空です: {path}")
        reader = csv.reader(ln for _i, ln in lines)
        header = [h.strip() for h in next(reader)]
        if "model" not in header or "input_per_mtok" not in header or "output_per_mtok" not in header:
            raise ValueError(f"単価表のヘッダに model / input_per_mtok / output_per_mtok が必要です: {path}")
        rows = []
        for (lineno, _ln), cells in zip(lines[1:], reader):
            row = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)}
            row["_line"] = lineno
            rows.append(row)
        return cls(rows, discount_override, source=os.path.abspath(path))

    def resolve(self, model: str) -> Optional[ModelPrice]:
        if model in self._cache:
            return self._cache[model]
        norm = normalize_model(model)
        found = self.models.get(norm)
        if found is None:
            for key in self._keys_longest_first:
                # "claude-opus-4" が "claude-opus-4-6" 等に誤って当たらないよう、区切り位置で前方一致
                if norm == key or norm.startswith(key + "-"):
                    found = self.models[key]
                    break
        self._cache[model] = found
        return found

    def cost(self, call) -> tuple[float, float]:
        """(定価ベース, 割引後) を返す。単価が無いモデルは (0, 0)。"""
        p = self.resolve(call.model)
        if p is None:
            return 0.0, 0.0
        total = sum(getattr(call, attr) * getattr(p, rate) for _n, attr, rate in TOKEN_KINDS)
        listed = total / PER
        if call.speed == "fast":
            listed *= p.fast_multiplier
        return listed, listed * (1.0 - p.discount_rate)

    def token_kind_costs(self, call) -> dict[str, float]:
        """トークン種別ごとの割引後金額（内訳表示用）。"""
        p = self.resolve(call.model)
        if p is None:
            return {n: 0.0 for n, _a, _r in TOKEN_KINDS}
        mult = p.fast_multiplier if call.speed == "fast" else 1.0
        return {n: getattr(call, a) * getattr(p, r) / PER * mult * (1.0 - p.discount_rate)
                for n, a, r in TOKEN_KINDS}
