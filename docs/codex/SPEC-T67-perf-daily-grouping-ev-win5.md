# SPEC-T67: perfダッシュボード EV通知実績・WIN5実績の日別グルーピング

作成: 2026-08-05 (Fable 5)。実装: jra-coder。レビュー・コミット: 上位モデル。

## 1. 目的

perfダッシュボード (jra_perf.py / index_perf.html) の「EV通知の実績」「WIN5の実績」を、
既存の「レース別の予測結果」(T56/T59d) と同じ**日別グルーピング表示**にする:

- 最新日: 日付サマリー行 + その日の明細行を展開表示
- 過去日: `<details>` 折りたたみ行 (サマリー行のみ)。開いたときに `api/perf?date=` で明細を遅延読込
- 日付指定ビュー (selected_date あり) は現行どおりのフラット表示のまま

ユーザー要望 (2026-08-05):「EV通知の実績、WIN5別実績は、レース別の予測結果のように
日ごとに結果をまとめてください」。

## 2. バックエンド (jra_perf.py)

### 2.1 全期間ビュー (compact_date が None) のみ payload に追加

既存の日別集計をそのまま組み替える。**新しいSQL・新しい母集団を作らない**:

- `ev_dates`: `ev_by_date` (馬単位に集約済み) を日付降順で
  `[{date(iso), n, settled, win, top3, tan_roi, fuku_roi}, ...]`。
  tan_roi/fuku_roi は既存 days 行と同じ算式 (settled 0 なら None)
- `win5_dates`: `win5_by_date` (各日 created_at 最新プラン) を日付降順で
  `[{date(iso), hits, total, all_settled, win5_hit,
     payout_fetched, payout_yen, shadow_hit(bool|None=算出不可), shadow_points}, ...]`

### 2.2 全期間ビューの明細行を最新日のみに絞る

- `ev.rows`: race_details と同様、**EV通知が存在する最新日の行のみ**返す
  (`ev_dates[0]` の日)。`ev.sum` (全期間サマリー) は現行のまま変えない
- `win5` リスト: **WIN5予測が存在する最新日の行のみ**返す (シャドー付与は現行どおり)。
  `win5_shadow_summary` は全期間集計のまま変えない
- 各セクションの「最新日」はセクションごとに独立に決める (レース・EV・WIN5で日付が違ってよい)

### 2.3 日付指定ビューは応答キー集合を変えない

`ev_dates` / `win5_dates` は **compact_date 指定時には含めない**
(tests/test_jra_perf.py の legacy-shape テストが日付指定時のキー集合を固定している。
race_dates と同じ扱い)。日付指定時の ev.rows / win5 は現行どおり全行。

## 3. フロントエンド (index_perf.html)

「レース別の予測結果」の実装 (`renderRaceResults` / `loadRaceDate` / `raceRowsByDate`) を
踏襲する:

- **EVセクション**: evSum の下のテーブルを日別化。日付サマリー行の文言例:
  `2026-07-26 (通知12頭 / 確定12 / 単勝的中2 回収85.5% / 複勝的中6 回収92.1%)`
- **WIN5セクション**: 日付サマリー行の文言例:
  `2026-08-02 (的中5/5 的中! 払戻33,820円 / 500点シャドー: 的中)`
  シャドー算出不可の日は `500点シャドー: 算出不可`、公式配当未取得は `払戻未取得`
- 過去日 `<details>` を開いたときの遅延読込は `api/perf?date=` を1回だけ叩き、
  **レスポンスを日付キーで共有キャッシュ**して3セクション (レース別/EV/WIN5) で使い回す
  (現行 `raceRowsByDate` はHTML文字列キャッシュ — payloadキャッシュに置き換えるか、
  セクション別HTMLキャッシュに拡張するかは実装に任せるが、同一日付への重複fetchはしない)
- 明細行の描画は既存関数 (`evBody` 行テンプレート / `w5Body` 行テンプレート+
  `win5ShadowLineHtml`) を関数化して再利用。**表示内容 (列・シャドー行) は変えない**
- 日付指定ビューでは現行どおりフラット表示

## 4. 変更してよいファイル / 禁止事項

- 変更可: `jra_perf.py`, `index_perf.html`, `tests/test_jra_perf.py`, 最小限の `style.css`
- 禁止: DB書き込み・スキーマ変更、jra_win5.py / jra_ev.py / 通知系の変更、
  `ev.sum`・`win5_shadow_summary`・`days` (日別実績)・グラフ系列の算式変更
- CRLF注意: HTMLをPythonスクリプトで編集しない。Edit/Writeツールで直接編集

## 5. テスト (tests/test_jra_perf.py)

1. 全期間ビュー: `ev_dates` / `win5_dates` が日付降順で入り、値が days 集計と整合する
2. 全期間ビュー: `ev.rows` / `win5` が最新日の行のみになる (複数日のfixtureで検証)
3. 日付指定ビュー: キー集合が現行と不変 (`ev_dates` / `win5_dates` を含まない)、
   ev.rows / win5 はその日の全行
4. 既存テスト全パス (`python -m pytest tests/ -q`、venv python)

## 6. 完了条件

- テスト全パス+リグレッションなし
- 実DB (data/jra_logging.db、読み取りのみ) で collect() を実行し、
  全期間ビューの ev_dates / win5_dates と最新日明細、日付指定ビューの互換を確認して
  出力例を報告に含める
- コミットはしない (上位モデルがレビュー後に行う)
