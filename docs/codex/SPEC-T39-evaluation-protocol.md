# SPEC T39: 評価プロトコル契約の固定

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T39。
背景: [RE-accuracy-ideas-20260717.md](RE-accuracy-ideas-20260717.md) §2/§8 (採用済み)。
起票: 2026-07-17 上位モデル。

**Codexの担当は、ヘルパー実装・実験台帳ツール・テストまで。freeze宣言・実験登録・
採否判断は上位モデルが行う。**

## 1. 目的

今後の全実験 (T41以降) を同一の契約で比較可能・リークフリーにする。固定するのは:

1. **共通cutoff**: 「購入可能時点に実在した情報だけを、同じcutoffで、市場・現行・候補へ
   公平に与える」ためのデータ選択規約
2. **prospective評価**: 2025・2026H1は既に多数の判断に再利用済みのため
   historical benchmarkに格下げ。最終採否は仕様freeze後の次開催日以降で行う
3. **実験台帳**: 事前登録なしの探索・事後の指標すり替えを構造的に防ぐ

## 2. 成果物

- `eval/cutoff.py` — 共通cutoffヘルパー
- `eval/blocks.py` — 開催日ブロックbootstrapヘルパー
- `eval/ledger.py` + `eval/experiments.jsonl` — 実験台帳 (append-only)
- `docs/evaluation-protocol.md` — 本契約の運用文書 (本SPECの§3-5を転記・整形)
- `tests/test_eval_protocol.py`

既存の backtest_*、本番コード (api/、jra_ev.py 等) は**変更しない**。
今後の新実験がこれらヘルパーをimportする。

## 3. 共通cutoff (`eval/cutoff.py`)

### 3.1 定義

- **WIN5用途**: 全5レースについて、**WIN5購入締切前に観測された最後のスナップショット**
  だけを使う。締切は保守的既定として「第1対象レース発走時刻の5分前」とし、
  定数 `WIN5_CUTOFF_MARGIN_MIN = 5` で設定可能にする (後半レースの直前オッズを
  WIN5予測に使うのはas-ofリーク)
- **単レースEV用途**: 各レースの通知時点 (使用スナップショットの `observed_at`)
- 両用途を**同じ特徴テーブルに混在させない**。ヘルパーは用途を引数で明示させる

### 3.2 API

```python
def select_snapshots(connection, race_ids, *, cutoff: datetime,
                     require_quality: bool = True) -> list[SnapshotRow]
def win5_cutoff(first_leg_post_time: datetime) -> datetime
```

- `observed_at <= cutoff` の最後の行を (race, horse) ごとに返す
- `require_quality=True` ではT40の品質フラグ (有効オッズ不足・重複取得・
  発走変更後の取得) を除外。除外件数を必ず返り値に含めて呼び出し側へ報告する
- cutoff後のデータを誤って返さないことをテストで保証 (境界値含む)

## 4. 開催日ブロックbootstrap (`eval/blocks.py`)

- paired比較のリサンプリング単位: 通常評価は**開催日** (race_id先頭8桁のJST日付)、
  WIN5は**WIN5開催日**。レース行単位のbootstrapは禁止 (同日相関の過小評価)
- `paired_block_bootstrap(metric_fn, blocks_a, blocks_b, n_resamples, seed)` —
  決定論 (seed固定)、差の分布とCI・p値を返す
- 逐次評価 (毎週見る) の扱いは台帳の停止条件に記載させる (実装はしない)

## 5. 実験台帳 (`eval/ledger.py`)

### 5.1 スキーマ (JSONL 1行=1実験、append-only)

```json
{
  "experiment_id": "T41-weightpack-v1",
  "registered_at_utc": "...",
  "commit_sha": "...",
  "data_hashes": {"ability_db": "...", "logging_db_rows": 123456},
  "features": ["...変換・欠損処理・利用可能時刻を含む記述..."],
  "primary_metric": "win_logloss_2124_cv",
  "safety_metrics": ["market_topk_floor", "win5_day_paired"],
  "search_grid": {"l2": [0.3, 1, 3]}, "candidate_count": 3,
  "stop_rule": "grid全評価後に固定、逐次確認なし",
  "benchmark_type": "historical",
  "prospective_start_date": null,
  "result_summary": null, "adjudication": null
}
```

### 5.2 規則

- **登録は実行前** (上位モデルが行う)。`primary_metric` はちょうど1つ
- 追記のみ。既存行の変更は `superseded_by` 参照で新行を追加する方式
- `ledger.py verify` で: JSONL構文・necessary keys・experiment_id一意・
  append-only性 (前回verify時の行数hashとの整合) を検査
- prospective宣言: `benchmark_type: "prospective"` の行に freeze commit と
  開始日 (次開催日) を必須化。宣言後の当該実験の仕様変更は新IDで登録

## 6. テスト

1. cutoff境界 (ちょうどcutoff時刻・直後・欠損observed_at) の選択が正しい
2. WIN5共通cutoffが後半レースの直前スナップショットを除外する
3. 品質フラグ除外と除外件数報告
4. ブロックbootstrapの決定論・ブロック単位性 (行シャッフルで結果不変)
5. 台帳: 必須キー欠落・primary_metric複数・重複IDの拒否、verifyの改竄検出
6. `python -m pytest tests/ -q` 全体パス

## 7. 受け入れ基準

- ヘルパーが本番コード無変更で動く (import方向は eval → 既存、逆は禁止)
- docs/evaluation-protocol.md が本契約を過不足なく記述
- 上位モデルが最初のfreeze宣言 (T45 baseline再固定) を台帳へ登録できる状態
