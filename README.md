# claude-usage-stats

Claude Code のローカルログ（`~/.claude/projects/**/*.jsonl`）から **トークン消費量と概算利用金額** を算出し、
**日次 / 月次 / ディレクトリ（プロジェクト）別 / モデル別 / チャット 1 往復（ターン）別** に可視化して
**HTML** と **xlsx** で出力するツールです。[ccusage](https://github.com/ryoppippi/ccusage) のログ解析・重複排除・
コスト計算の考え方をベースに、ターン単位の集計と Excel 出力、自動更新（Stop フック）を加えています。

- Windows / macOS / Linux で動作（Python 3.9+、標準ライブラリ + XlsxWriter のみ）
- ネットワーク不要（価格表は同梱の `pricing.csv`。社内ネットワーク・オフラインで動作）
- HTML は外部 CDN 不要の単体ファイル。期間・プロジェクト・モデルで絞り込みでき、ライト/ダーク両対応

```
~/.claude/projects/**/*.jsonl ──► 解析・重複排除 ──► SQLite に蓄積 ──► ターンに割当・金額計算 ──► HTML / xlsx
      (Claude Code のログ)         (logs.py)          (store.py)        (aggregate.py, pricing.py)
```

---

## 1. セットアップ

```bash
# macOS / Linux
python3 -m pip install -r requirements.txt

# Windows (PowerShell / コマンドプロンプト)
py -3 -m pip install -r requirements.txt
```

`requirements.txt` は `XlsxWriter`（xlsx 出力用）と、Windows のみ `tzdata`（`--timezone Asia/Tokyo` の解決用）です。
HTML だけ使うなら何もインストールしなくても動きます（`--formats html`）。

## 2. クイックスタート

```bash
python3 claude_usage.py --timezone Asia/Tokyo        # Windows: py -3 claude_usage.py --timezone Asia/Tokyo
```

コンソールに月別・プロジェクト別・モデル別のサマリーが出て、次の 2 ファイルが生成されます。

| ファイル | 内容 |
|---|---|
| `~/.claude-usage-stats/reports/claude-usage-report.html` | ブラウザで開くダッシュボード |
| `~/.claude-usage-stats/reports/claude-usage-report.xlsx` | Excel ブック（8 シート、ネイティブグラフ付き） |

出力先は `--out-dir DIR` で変更できます。データの置き場（キャッシュ DB・既定の出力先）は環境変数
`CLAUDE_USAGE_HOME` で変更できます（既定 `~/.claude-usage-stats`、Windows は `%USERPROFILE%\.claude-usage-stats`）。

## 3. チャット 1 往復ごとに自動更新する（Stop フック）

Claude Code の応答が終わるたびにレポートを更新するには、Stop フックを登録します。

```bash
python3 claude_usage.py install-hook --timezone Asia/Tokyo
```

- `~/.claude/settings.json`（`CLAUDE_CONFIG_DIR` があればそちら）に Stop フックを **1 件追加** します。
  既存の設定・他のフックには触れず、書き換え前に `settings.json.bak.<日時>` を作成します。何度実行しても重複しません。
- `install-hook` に付けた `--timezone` や `--formats html` などのオプションは、そのままフックのコマンドに引き継がれます
  （不正な値は登録前に弾きます）。
- 登録後は **Claude Code を再起動するか `/hooks` を一度開く** と反映されます。
- プロジェクト単位にしたい場合は `--scope project`（`./.claude/settings.json`）、ファイルを直接指定する場合は `--settings FILE`。
  `--dry-run` で書き込まずに内容だけ確認できます。
- 解除: `python3 claude_usage.py uninstall-hook`

フックの動作:

- 変更のあったログファイルだけを再解析する増分更新です（実測: 初回 約 0.6 秒、2 回目以降 約 0.2 秒 / 6,000 呼び出し規模）。
- 何があってもセッションを妨げません（常に終了コード 0）。失敗は `~/.claude-usage-stats/hook.log` に記録されます。
- フックが連続発火しても二重実行しません（簡易ロック）。
- 大規模な履歴で xlsx の生成が重い場合は `install-hook --formats html` として HTML だけ自動更新にし、
  xlsx は必要なときに手動で出力する運用が軽量です。

## 4. コマンドとオプション

```
python claude_usage.py [report] [オプション]     集計してレポート生成（サブコマンド省略時）
python claude_usage.py install-hook [--scope user|project] [--settings FILE] [--dry-run] [report のオプション]
python claude_usage.py uninstall-hook [--scope ...] [--settings FILE] [--dry-run]
python claude_usage.py hook                     Stop フックから呼ばれる（通常は直接実行しない）
```

`report` のオプション:

| オプション | 説明 |
|---|---|
| `--timezone TZ` | 集計タイムゾーン（`Asia/Tokyo`, `UTC` など）。省略時は OS のローカル。日・月の区切りに影響 |
| `--since YYYY-MM-DD` / `--until YYYY-MM-DD` | 期間で絞り込み |
| `--days N` | 直近 N 日（今日を含む） |
| `--project TEXT` | 作業ディレクトリのパスに TEXT を含むものだけ |
| `--model TEXT` | モデル ID に TEXT を含むものだけ |
| `--pricing FILE` | 単価表 CSV を指定（既定: 同梱 `pricing.csv` / 環境変数 `CLAUDE_USAGE_PRICING`） |
| `--discount RATE` | 契約割引率を **小数** で（15% 引き = `0.15`）。`pricing.csv` の指定より優先 |
| `--formats html,xlsx` | 出力形式。`none` で画面表示のみ |
| `--out-dir DIR` | 出力先ディレクトリ |
| `--prompt-chars N` | ターン明細にプロンプト先頭 N 文字を載せる（既定 0 = 載せない） |
| `--claude-dir DIR` | ログの親ディレクトリ（`DIR/projects` を読む）。複数指定可 |
| `--no-sync` | ログを再走査せず蓄積済みデータだけで集計 |
| `--rebuild` | 蓄積データを破棄して全ログを再解析（**元ログが消えた履歴は失われます**） |
| `-q` | コンソール出力なし |

ログの場所は `--claude-dir` → 環境変数 `CLAUDE_CONFIG_DIR`（カンマ区切りで複数可）→ `~/.config/claude` と `~/.claude` の順で決まります
（ccusage と同じ規則）。

使用例:

```bash
python3 claude_usage.py --days 30 --timezone Asia/Tokyo             # 直近 30 日
python3 claude_usage.py --since 2026-09-01 --until 2026-09-30       # 9 月分
python3 claude_usage.py --project my-app --formats xlsx             # 特定プロジェクトの xlsx だけ
python3 claude_usage.py --discount 0.15                             # 15% 割引を適用
python3 claude_usage.py --prompt-chars 60                           # 高コストなターンの中身を確認したいとき
```

## 5. レポートの見方

### HTML

上部の絞り込み行（期間プリセット / 日付指定 / プロジェクト / モデル / 指標＝金額⇔トークン）が **ページ内の全グラフと表に適用** されます。

| 区画 | 内容 |
|---|---|
| ヒーロー・KPI | 概算利用金額、ターン数、API 呼び出し数、合計トークン、キャッシュ、1 ターン平均、1 ターン平均所要時間、稼働日あたり |
| 日次の推移 | モデル別の積み上げ縦棒。棒にカーソルを合わせると当日の内訳と合計 |
| 月次の推移 | 同上（月単位） |
| モデル別 / ディレクトリ別 | 横棒（上位 15 プロジェクト、残りは「その他」）。カーソルでターン数・呼び出し数なども表示 |
| トークン種別の内訳 | 入力 / 出力 / キャッシュ書込（5 分・1 時間）/ キャッシュ読込ごとのトークン数と金額 |
| ターン一覧 | チャット 1 往復ごとの消費量と所要時間。見出しクリックで並べ替え（既定: 金額の高い順） |

各グラフの下の「表で見る」で同じ数値を表として確認できます（ツールチップに依存せず読めます）。
右上のボタンで配色（自動 / ライト / ダーク）を切り替えられます。

### xlsx

| シート | 内容 |
|---|---|
| サマリー | 期間・価格基準・総額・KPI・トークン種別の内訳・警告 |
| 日次 / 月次 | 集計表（フィルタ・並べ替え・合計行つき）+ 金額の棒グラフ |
| プロジェクト別 | ディレクトリ別の集計表 + 上位 15 件の棒グラフ |
| モデル別 | モデル別の集計表 + 構成比の円グラフ |
| クロス集計 | 月×プロジェクト、月×モデル、日×モデルの金額マトリクス |
| ターン明細 | 1 ターン 1 行（開始日時・プロジェクト・セッション・モデル・トークン各種・金額・所要時間）。ピボットの元データ向け |
| 単価表 | 計算に使った単価・倍率・割引率 |

集計表の列: ターン数 / API 呼出数 / 入力 / 出力 / キャッシュ書込(5 分・1 時間) / キャッシュ読込 / 合計トークン /
定価ベース / 割引額 / **概算金額**。日次・月次の「ターン数」は、日や月をまたいだターンが両方に数えられるため、
合計行の値が全体のターン数（サマリー）より大きくなることがあります。

## 6. 集計の定義

**ターン（チャット 1 往復）** — 人間がプロンプトを送ってから、次にプロンプトを送るまでの間に発生した全 API 呼び出し。
ツール実行のたびの再呼び出しと、サブエージェントの消費も含みます。ツール結果・スラッシュコマンドの出力・
バックグラウンド通知・圧縮要約などはターンの開始とみなしません。呼び出しは「同一セッション内で、直前のプロンプト」に
時刻で割り当てるため、サブエージェントのログが別ファイルでも正しく合算されます。

**所要時間** — プロンプトを送信してから、そのターンの最後の応答が出力されるまでの時間。終了時刻は、ターン内の
メインスレッドの最後の API 呼び出しの記録時刻です（Stop フックの発火時刻と数十ミリ秒の差で一致することを実ログで確認済み）。
蓄積済みの API 呼び出しの時刻から計算するため、**過去の記録も追加の操作なしで復元され**、元のログが削除済みでも同様です。
注意点:
- ツール実行の許可待ち・質問への回答待ちなど、人間の待ち時間も含みます（「送信から出力終了まで」の実時間）。
- 後からバックグラウンドで動くサブエージェントは終了時刻を延ばしません（消費量は従来どおりターンに合算）。
- プロンプトが不明なターン（ログにプロンプトが無く呼び出しだけ残っているもの）は算出せず、空欄（HTML は「-」）にします。平均は算出できたターンだけで計算します。
- 所要時間はターン単位の値です。プロジェクト・モデル・期間で絞り込んだときは、該当するターンの所要時間を全体（ターンの開始から終了まで）で数えます。

**プロジェクト（ディレクトリ）** — セッションを開始した作業ディレクトリ（ログの `cwd`）。同名ディレクトリが複数あるときは
親ディレクトリも付けて区別します。

**重複排除** — ccusage と同じく `message.id` + `requestId` で重複を除きます（ストリーミング中の同一応答が複数行に書かれる、
セッション再開でログが複製される、といった二重計上を防ぎます）。`<synthetic>`（API エラー等の疑似応答）は除外します。

**トークン種別** — 入力 / 出力（思考トークンを含む）/ キャッシュ書込（5 分・1 時間を別単価）/ キャッシュ読込。
「合計トークン」はこの 5 種の合計（ccusage の totalTokens と同じ）です。

**Fast モード** — `usage.speed == "fast"` の呼び出しは単価が異なるため、`claude-opus-5-5 (fast)` のように
**別モデルとして** 集計し、`pricing.csv` の `fast_multiplier` を掛けます。

**タイムゾーン** — 保存は UTC のまま、集計時に指定タイムゾーンへ変換して日・月を区切ります。
UTC と JST では日付がずれるため、他システムの数値と突き合わせる際は揃えてください。

## 7. 金額の計算と単価表（pricing.csv）

金額 = 各トークン数 × 単価（USD / 100 万トークン）の合計 →（Fast なら倍率）→ **割引適用**。
ログに `costUSD` があっても使わず、常にトークン数から再計算します。

単価表は CSV です（Excel で編集して UTF-8 で保存できます。BOM 付き UTF-8 も可）。`#` で始まる行はコメントです。

```csv
model,input_per_mtok,output_per_mtok,cache_write_5m_per_mtok,cache_write_1h_per_mtok,cache_read_per_mtok,fast_multiplier,discount_rate,note
*,,,,,,,0.15,全モデルの既定の割引率（15% 引き）
claude-opus-5-5,4.0,20.0,,,0.20,2.0,,
claude-sonnet-5-5,2.0,10.0,,,,,,
claude-haiku-4-5,1.0,5.0,,,,,0.20,この行だけ 20% 引き
```

| 列 | 意味 |
|---|---|
| `model` | モデル ID の前方一致キー。`*` の行は「全モデルの既定の割引率」を `discount_rate` に書くための特別行 |
| `input_per_mtok` / `output_per_mtok` | 入力 / 出力の単価（必須） |
| `cache_write_5m_per_mtok` / `cache_write_1h_per_mtok` / `cache_read_per_mtok` | キャッシュの単価。**空欄なら入力単価の 1.25 倍 / 2 倍 / 0.1 倍** |
| `fast_multiplier` | Fast モード時の倍率（空欄 = 1.0） |
| `discount_rate` | 契約割引率。**`0.15` = 15% 引き**（`15` と書くと行番号つきのエラー）。空欄は `*` 行の値（無ければ 0） |
| `note` | 備考（カンマを含める場合は `"…"` で囲む） |

- モデル名は **前方一致（最長一致）**。`claude-haiku-4-5-20251001` や Bedrock/Vertex 形式（`us.anthropic.claude-…-v1:0`,
  `…@20250514`）も同じ行に当たります。
- **Claude Enterprise の契約単価・割引** は、`*` 行（全モデル）かモデル行の `discount_rate`、または単価そのものの書き換えで反映します。
  一時的に試すだけなら `--discount 0.15`（表の割引指定より優先）。レポートには「定価ベース / 割引額 / 概算金額」を並べて表示します。
- 単価表に無いモデルは **金額 0 で集計され、警告が出ます**（コンソール・HTML・xlsx）。新モデルが出たら行を追加してください。
  未知のマイナーバージョン（例: `claude-sonnet-5-7`）は、前方一致で近い系列（`claude-sonnet-5`）の単価に当たる点に注意してください。
- 同梱の単価は 2026-09-25 時点の公開 API 価格です。契約内容や改定に合わせて必ず確認・更新してください。

> 金額はあくまで **ログのトークン数からの概算** です。請求額とは、丸め・契約条件・ログに残らない利用の有無により差が出ます。
> 正式な請求額は Anthropic の請求画面・管理コンソールで確認してください。

## 8. データの保存とプライバシー

| 場所 | 内容 |
|---|---|
| `~/.claude-usage-stats/usage.db` | 解析結果の蓄積（SQLite）。API 呼び出しごとのトークン数・モデル・時刻・cwd と、プロンプト先頭 200 文字のプレビュー |
| `~/.claude-usage-stats/reports/` | 生成したレポート |
| `~/.claude-usage-stats/hook.log` | フックのエラー記録（あれば） |

- Claude Code の設定やクリーンアップ（`cleanupPeriodDays` など）で古いログが削除されても、`usage.db` に蓄積済みの分は
  残るため、月次・年次の集計を続けられます。**定期的にバックアップする場合は `usage.db` を保存してください。**
  `--rebuild` は蓄積を破棄するので、元ログが消えている期間の履歴が失われます。
- **プロンプト本文はレポートに出力しません**（既定）。`--prompt-chars N` を指定した場合のみターン明細に先頭 N 文字を載せます。
  レポートを共有する場合は既定のまま使うことを推奨します。
- ディレクトリのパス（ユーザー名を含みうる）はレポートに載ります。共有時はご注意ください。
- 通信は一切行いません。

## 9. 制限事項

- 対象は **その端末に残った Claude Code のログ** です。次のものは含まれません: 他の端末での利用（端末ごとにレポートを出し、
  xlsx を突き合わせてください）、claude.ai / デスクトップアプリの通常チャット、API を直接呼ぶアプリ、ログが削除された期間
  （`usage.db` に蓄積される前のもの）。
- 期間別の単価切り替え（値上げ・値下げをまたぐ集計）には未対応で、常に現在の `pricing.csv` で計算します。
- 100 万トークンを超える長文脈の割増料金など、モデル固有の段階料金は扱いません（`pricing.csv` の単価は一律）。
- 通貨は USD 固定です（円換算なし）。
- Claude Code のトランスクリプトは **非公開仕様** です。バージョンアップで形式が変わると集計に影響しうるため、
  依存している前提を `cusage/logs.py` の docstring に列挙してあります。異常に気付いたら最初にそこを確認してください。
- HTML には全ターンのデータを埋め込みます。数十万ターン規模ではファイルが大きく（数十 MB）なるため、
  `--days` / `--since` で範囲を絞ってください。

## 10. トラブルシュート

| 症状 | 対処 |
|---|---|
| `Claude Code のログが見つかりません` | ログの場所を `--claude-dir` か `CLAUDE_CONFIG_DIR` で指定。`<DIR>/projects` が存在する必要があります |
| 金額が 0 / `単価表に無いモデル` の警告 | `pricing.csv` にそのモデルの行を追加 |
| `割引率は 0 以上 1 未満の小数で…` | `15` ではなく `0.15` と指定 |
| `タイムゾーン '…' を解決できません` | Windows は `pip install tzdata`。名前は IANA 形式（`Asia/Tokyo`） |
| `xlsx を書き込めません` | Excel でレポートを開いたままになっていないか確認（Windows はロックされます） |
| 自動更新されない | `install-hook` 後に Claude Code を再起動 / `/hooks` を開く。`~/.claude-usage-stats/hook.log` を確認。`settings.json` の command に書かれた Python が存在するか確認（仮想環境を切り替えたら `install-hook` を再実行） |
| 数字が ccusage と違う | タイムゾーン（`--timezone` を揃える）、`pricing.csv` の単価（ccusage は LiteLLM の価格表。未知モデルは 0 になる）、`--project` などの絞り込みを確認 |
| 日付が 1 日ずれて見える | 集計タイムゾーンの違い。`--timezone` を明示 |
| 文字化け（Windows コンソール） | ツール側で UTF-8 に切り替えますが、古い環境では `set PYTHONUTF8=1` を併用 |

## 11. ccusage との関係

| | ccusage | このツール |
|---|---|---|
| ログ探索・重複排除（`message.id`+`requestId`）・`<synthetic>` 除外 | ○ | 同じ規則 |
| 集計単位 | 日 / 月 / セッション / 5 時間ブロック | 日 / 月 / プロジェクト / モデル / **ターン** |
| 単価 | LiteLLM 価格表（ネットワーク取得） | 同梱 `pricing.csv`（オフライン・割引対応） |
| 出力 | ターミナル表 / JSON | **HTML ダッシュボード / xlsx** |
| 元ログ削除後の履歴 | 残らない | SQLite に蓄積して保持 |
| 自動更新 | - | Stop フック |

検証: 開発マシンの実ログで `ccusage daily --offline --timezone Asia/Tokyo` と比較し、比較可能な 16 日分は
入力/出力/キャッシュ各トークン数と金額が完全に一致しました（残る 1 日は比較中も追記され続けていた当日分と、
ccusage 側の価格表に無いモデルによる差）。

## 12. 開発

```
claude_usage.py         CLI（report / hook / install-hook / uninstall-hook）
pricing.csv            単価表（編集して運用）
cusage/logs.py          ログ探索・解析（トランスクリプト仕様への依存はここに集約）
cusage/store.py         SQLite への増分同期・履歴保持
cusage/pricing.py       単価の解決と金額計算
cusage/aggregate.py     ターン割当と日/月/プロジェクト/モデル集計
cusage/report_html.py   HTML 生成（templates/report.html にデータを埋め込む）
cusage/report_xlsx.py   xlsx 生成
cusage/hook.py          settings.json の安全な編集・ロック
tests/test_usage.py     テスト（標準の unittest。実ログには触れない）
```

```bash
python3 tests/test_usage.py
```

テストは合成ログで、重複排除・ターン判定・サブエージェント合算・JST での日付跨ぎ・Fast/割引・単価未設定・
増分同期と履歴保持・フックの安全な編集・HTML への埋め込みの安全性・xlsx の構造を確認します。
動作確認は macOS で行っています。Windows / Linux はパス・引用符・`tzdata` などの OS 依存箇所を考慮して実装していますが、
実機での確認は未実施です。問題があれば `hook.log` とコンソール出力を添えて報告してください。
