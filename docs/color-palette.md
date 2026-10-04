# カラーパレット（Material Design 3）

HTML レポート（`cusage/templates/report.html`）の配色。シードカラー **#F5B0A3**（HCT: 色相 29 / 彩度 27 / トーン 78）から、
Material の TonalSpot スキーム（2021 仕様、primary 彩度 36・secondary 16・tertiary 24・neutral 6・neutral variant 8）で生成し、
M3 標準のトーン割り当て表でカラーロールに落とし込んでいる。

## トーナルパレット

| トーン | Primary | Secondary | Tertiary | Neutral | Neutral variant |
|---|---|---|---|---|---|
| 100 | `#ffffff` | `#ffffff` | `#ffffff` | `#ffffff` | `#ffffff` |
| 99 | `#fffbff` | `#fffbff` | `#fffbff` | `#fffbff` | `#fffbff` |
| 95 | `#ffede9` | `#ffede9` | `#ffefd0` | `#ffede9` | `#ffede9` |
| 90 | `#ffdad4` | `#ffdad4` | `#fae0a6` | `#f1dfdb` | `#f5ddd9` |
| 80 | `#ffb4a6` | `#e7bdb5` | `#ddc48c` | `#d4c3c0` | `#d8c2be` |
| 70 | `#ea9585` | `#caa29b` | `#c0a973` | `#b8a8a5` | `#bca7a3` |
| 60 | `#cc7b6c` | `#ae8881` | `#a48e5b` | `#9d8d8b` | `#a08c89` |
| 50 | `#ad6355` | `#926f68` | `#897544` | `#827471` | `#857370` |
| 40 | `#904b3e` | `#775650` | `#6f5c2e` | `#685b59` | `#6c5b57` |
| 30 | `#733429` | `#5d3f3a` | `#554519` | `#504442` | `#534340` |
| 20 | `#561e15` | `#442a24` | `#3d2e04` | `#392e2c` | `#3b2d2b` |
| 10 | `#3a0a04` | `#2c1511` | `#251a00` | `#231918` | `#251916` |
| 0 | `#000000` | `#000000` | `#000000` | `#000000` | `#000000` |

## カラーロール

| ロール | CSS 変数 | ライト | ダーク |
|---|---|---|---|
| primary | `--primary` | `#904b3e` | `#ffb4a6` |
| on_primary | `--on-primary` | `#ffffff` | `#561e15` |
| primary_container | `--primary-container` | `#ffdad4` | `#733429` |
| on_primary_container | `--on-primary-container` | `#3a0a04` | `#ffdad4` |
| secondary_container | `--secondary-container` | `#ffdad4` | `#5d3f3a` |
| on_secondary_container | `--on-secondary-container` | `#2c1511` | `#ffdad4` |
| tertiary_container | `--tertiary-container` | `#fae0a6` | `#554519` |
| on_tertiary_container | `--on-tertiary-container` | `#251a00` | `#fae0a6` |
| surface | `--surface` | `#fff8f6` | `#1a1110` |
| surface_container_low | `--surface-low` | `#fff0ee` | `#231918` |
| surface_container | `--surface-mid` | `#fceae6` | `#271d1c` |
| surface_container_high | `--surface-high` | `#f7e4e1` | `#322826` |
| on_surface | `--on-surface` | `#231918` | `#f1dfdb` |
| on_surface_variant | `--on-surface-variant` | `#534340` | `#d8c2be` |
| outline | `--outline` | `#857370` | `#a08c89` |
| outline_variant | `--outline-variant` | `#d8c2be` | `#534340` |

ロール名と CSS 変数名は、短くするため一部（`surface-container-*` → `surface-low/mid/high`）を省略している。

## グラフのカテゴリ色

primary と同じ HCT（ライト: トーン 50、ダーク: トーン 74、彩度 36〜40）で色相だけ変えた 8 色 + 「その他」。
`--s1` が primary 系（サーモン）で、単色の棒グラフはこれを使う。

| 変数 | 色相 | ライト | ダーク |
|---|---|---|---|
| `--s1` | 29 | `#ad6355` | `#f79f8f` |
| `--s2` | 258 | `#4e79b1` | `#8eb8f4` |
| `--s3` | 152 | `#458453` | `#83c58d` |
| `--s4` | 85 | `#967117` | `#dbb052` |
| `--s5` | 318 | `#9167a6` | `#d4a5e9` |
| `--s6` | 195 | `#008583` | `#61c6c3` |
| `--s7` | 350 | `#b4607f` | `#f2a0bd` |
| `--s8` | 120 | `#707d2e` | `#afbe66` |
| `--s-other` | 29（彩度 6） | `#a29390` | `#7d6f6c` |

## モデルの色分け

モデルは系統（opus / sonnet / haiku / fable …、`report_html.model_family`）ごとに色相、同一系統のバージョン違いはトーンで区別する。

- 色相: 全期間の金額が最も大きい系統が `--s1`（primary 系）、以降は金額順に `--s2`〜`--s8`。9 系統目以降は `--s-other`。
- トーン: 系統内で金額が大きい順に、基準色へ `--tone-mix`（ライト: 白 / ダーク: 黒）を 0 / 16 / 30 / 42 / 52 % 混ぜる（`color-mix(in oklab, …)`）。
  1 系統 6 バージョン目以降は `--s-other`。
- 割り当ては絞り込みで変わらない（`build_payload` が `modelColor` として埋め込む）。

## 再生成

Python の [`materialyoucolor`](https://pypi.org/project/materialyoucolor/) で再現できる（`spec_version="2021"` を指定する。既定は 2025 仕様で彩度が異なる）。

```python
from materialyoucolor.scheme.scheme_tonal_spot import SchemeTonalSpot
from materialyoucolor.hct import Hct

sc = SchemeTonalSpot(Hct.from_int(0xFFF5B0A3), False, 0.0, spec_version="2021")
p = sc.primary_palette; print(hex(p.tone(40)))   # ライトの primary
```
