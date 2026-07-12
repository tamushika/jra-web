# SPEC T31: 2026年1〜4月の runs 欠落を netkeiba から埋め戻す

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T31。

## 背景
ability.db の runs テーブルは Neon 延伸 (`extend_ability_from_neon.py`) 開始前の期間が
欠落している: **2026年1〜3月が0件、4月が1,003行のみ (通常月は3,500〜4,500行)**。
このため全ての「2026H1 OOS」検証が実質4〜6月分だった。netkeiba の確定成績ページから
runs 行を再構成して埋め戻す。TARGETエクスポートは不可 (ユーザー確認済み) のため
スクレイプが唯一の手段。

## 参考にする既存コード (必読)
- `backfill_fukusho_netkeiba.py`: `fetch_race_ids(date8, session)` (開催日→race_id対応表)、
  UA、EUC-JPデコード、`_split_br`
- `backfill_pay_netkeiba.py`: 払戻テーブル (pay_table_01) のパース、照合ロジック
- `extend_ability_from_neon.py`: runs 26列の**正確な列定義とフォーマット**
  (COLS リスト、win_pay の TARGET互換形式、compute_pci、列名明示INSERT)
- `build_ability_db.py`: `parse_race_class`

## 要求仕様

### 新規スクリプト `backfill_runs_netkeiba.py`
1. 対象: `--from 20260101 --to 20260430` (既定値)。開催日ごとに race_id を解決し、
   **runs に既に存在する (date, place, r) はスキップ** (4月の既存1,003行と重複させない。冪等)。
2. db.netkeiba.com の成績ページから各馬の行を構成する。列の対応:
   - rank(着順、取消/除外/中止は行ごと除外 — extend_ability と同じ「rank>=90相当は入れない」方針)、
     umaban、horse(馬名)、sex/age(性齢を分解)、jockey、kinryo(斤量)、
     time_sec(タイム "1:23.4"→秒。scoring.parse_run_time 使用)、
     c4(通過の最終コーナー順位)、agari(上り3F)、popularity(人気)、weight(馬体重 "472(+2)"→472)、
     affi(調教師欄の [東]→美浦 / [西]→栗東)、total_horses(行数から)
   - レースヘッダから: race_name、track_type(芝/ダート。**障害レースはレースごと除外**)、
     distance、condition(良/稍/重/不 の1文字)、race_class=parse_race_class(race_name)
   - pci: extend_ability_from_neon.compute_pci と同一式で計算
   - **win_pay (重要・TARGET互換)**: 勝ち馬=単勝払戻円の文字列 (例 '260')、
     他馬=確定オッズを括弧書き (例 '(3.7)')。払戻は同ページの pay_table_01 単勝から、
     オッズは成績表の単勝オッズ列から取る
   - fukusho_pay: 複勝払戻を該当馬 (最大3頭) に付与
   - chakusa: None (Neon延伸行と揃える)
3. **照合 (必須)**: 勝ち馬の「単勝オッズ×100 ≒ 単勝払戻」(±10円) を確認し、
   合わないレースは保存せずスキップして件数報告 (race_id 取り違え防止。
   backfill_pay_netkeiba と同じ発想)
4. INSERT は**列名明示** (runs は列が増えることがある。extend_ability の COLS を参照)。
   レース単位でコミット。1リクエスト1.2秒スリープ (対象約1,200レース+一覧≒40分)
5. `--dry-run --limit 3` で動作確認できること

### 完了後の後続処理 (スクリプト内でなく手順として報告に含める)
6. `python backfill_pay_netkeiba.py --from 20260101 --to 20260430` を実行
   (race_payouts の同期間分。runs が入った後でないと動かない)
7. 検証クエリの結果を報告:
   - 月別行数 (202601〜202604 が各3,500〜4,500行になること)
   - win_pay 充填率 (>99%)、fukusho_pay 件数 (レース数×3前後)
   - 既存4月行との突き合わせ: 同一レースが二重登録されていないこと
   - 新期間の馬の horse_pedigree カバレッジ (低ければ件数を報告 —
     血統バックフィルの追加実行要否は上位モデルが判断)

## 受け入れ基準
- 202601〜03 が通常月並みの行数になり、202604 の欠落分が補完される
- 照合失敗率が数%以内 (超える場合は原因を報告して中断)
- 既存行の変更・削除がゼロ (スキップ方式)
- `python -m pytest tests/ -q` が壊れていない

## やらないこと
- 2025年以前の再取得 (TARGET由来の凍結データは触らない)
- Neon への書き込み
- 他の大量スクレイプタスクとの同時実行 (日次制限の共有に注意)
