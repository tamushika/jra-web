# SPEC T40: Phase C オッズスナップショットの品質強化

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T40。
背景: [RE-accuracy-ideas-20260717.md](RE-accuracy-ideas-20260717.md) §4.1 (採用済み)。
起票: 2026-07-17 上位モデル (コード裏取り済み)。

**⚠️ 最重要制約: 通知の発火条件・文言・送信経路 (`refresh_and_alert`、checked15/5分岐、
LINE/Discord/ブラウザ) は一切変更しない。** 変更は snapshot記録経路と logging層に限定し、
上位モデルがdiffで確認する。

## 1. 現状の問題 (2026-07-17 上位モデルがコードで確認)

`jra_ev.py` の Phase C スナップショット (`_SNAPSHOT_STAGES`、`snapshot_odds`、
`scheduler_loop`) と `api/logging_store.py` の `odds_snapshots` について:

1. **stage=30/10/2 はラベルであり実取得時刻を保証しない**。発火条件は
   `remain <= window_sec` のため、プロセスを遅れて起動すると checked30/10/2 が
   短時間に連続発火し、例えば残り8分の取得が stage=30 として記録され得る
2. `odds_snapshots` には `observed_at`/`source_updated_at`/`stage` はあるが、
   **取得時点に認識していた予定発走時刻**と **seconds_to_post_at_observation** が無く、
   後から「そのstageの実態」を復元できない (発走時刻変更があると特に不可能)
3. `snapshot_odds` は `analyze_one` が成功すれば **有効オッズが不足していても
   成功扱い** (checkedフラグが立ち、そのstageは再取得されない)
4. 発走時刻変更・スケジューラ再起動・同stage近接重複の記録が無い

T17 (時点別オッズ特徴) はこのデータを学習に使うため、蓄積が進む前の修正が急務。

## 2. スキーマ拡張 (`api/logging_store.py`)

`odds_snapshots` へ列追加する。既存DBには `stage` 列追加と同じ
`PRAGMA table_info` → `ALTER TABLE ADD COLUMN` パターンで後方互換に適用する
(環境の落とし穴: **INSERTは列名明示**を維持):

| 列 | 型 | 内容 |
|---|---|---|
| `scheduled_post_at` | TEXT | 取得時点に認識していた予定発走時刻 (JST ISO) |
| `seconds_to_post` | REAL | `scheduled_post_at - observed_at` (秒、取得時計算) |
| `fetch_duration_ms` | INTEGER | 取得開始→解析完了の所要 |
| `valid_odds_count` | INTEGER | 有効単勝オッズを持つ頭数 |
| `field_size` | INTEGER | 出走頭数 (取消除く) |

品質フラグは既存 `data_quality_flags_json` に追記する (新列にしない):

- `late_capture`: `seconds_to_post` が stageの想定窓 (stage分×60 ± 60秒) を外れた
- `catchup_burst`: 同一レースで直前120秒以内に別stageのsnapshotを取得済み
  (遅延起動のまとめ取り)
- `insufficient_odds`: `valid_odds_count < max(4, field_size // 2)`
- `post_time_changed`: 取得時の `scheduled_post_at` が同レースの前回snapshot時と異なる
- `scheduler_restart`: プロセス起動後の最初のスキャンサイクル内での取得

## 3. 記録経路 (`jra_ev.py`)

- `snapshot_odds` → `analyze_one` → logging の経路で§2の値・フラグを算出して渡す。
  `rec["_start_dt"]` を `scheduled_post_at` の情報源とし、変更検知は
  レコード内に前回値を保持して比較する
- **`insufficient_odds` の場合は成功扱いにしない**: checkedフラグを立てず、
  窓内 (giveup境界まで) は次サイクルで再試行する。giveup境界到達時は
  フラグ付きで保存し checked を立てる (現行のgiveup設計は維持 —
  15分/5分の通知付き判定を絶対にブロックしない)
- `scheduler_loop` の通知分岐 (checked15/checked5 の if/elif) は**無変更**。
  snapshot用ifブロック内の変更のみ許す

## 4. 既存蓄積分の監査スクリプト (`audit_odds_snapshots.py`、READ-ONLY)

蓄積済みデータがT17に使えるかを上位モデルが判断するためのレポートを出力する:

1. stage別の実取得時刻分布: `observed_at` と結果データの発走時刻から
   実残り分を復元し、stageラベルとの乖離ヒストグラム (新列が無い過去分は
   monitor state / 結果テーブルから可能な範囲で復元し、復元不能件数を明示)
2. 同一レース内の近接重複取得 (catchup burst) の件数・日別分布
3. レース/stage別カバレッジ (取得欠損率)、有効オッズ頭数の分布
4. 発走時刻変更が疑われるレースの一覧
5. サマリ: stage別の「窓内取得率」— T17で使える clean subset の規模

出力: markdown + CSV を `outputs/t40/` へ。本番DBへの書き込みなし。

## 5. テスト

1. 列追加migrationが新規DB・既存DB (列なし) の両方で動く。列名明示INSERT維持
2. `seconds_to_post`・各品質フラグの算出 (合成レコードで境界値含む)
3. `insufficient_odds` 時に checkedフラグが立たず窓内で再試行し、
   giveup境界でフラグ付き保存に切り替わる
4. **通知経路の非回帰**: `refresh_and_alert`・checked15/5 分岐に関する
   既存テストが無修正でパスすること。`snapshot_odds` がアラート・通知を
   一切生成しないことの既存保証を維持
5. 監査スクリプトが合成DBで正しい集計を返す (READ-ONLYであること)
6. `python -m pytest tests/ -q` 全体パス

## 6. 受け入れ基準

- 新規snapshotに §2 の値・フラグが記録される
- オッズ不足時の空振り成功が無くなる (窓内再試行)
- 監査レポートで既存蓄積の使用可否を判断できる
- 通知系diffゼロ (記録経路の引数追加を除く)
