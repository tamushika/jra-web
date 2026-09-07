# SPEC-T72: 道悪適性ファクターの芝/ダート分離 (same_surface_only)

起票: 2026-09-07 (Fable 5)。対象: `api/scoring.py` `eval_wet_aptitude` (Webスコア/WIN5スコアの道悪適性項目) と `backtest_wet.py`。

## 1. 背景 (不具合)
`eval_wet_aptitude` は直近4走の馬場状態 (良/稍/重/不) だけで「道悪ベスト着順 vs 良ベスト着順」を比較し、芝・ダートを区別しない。
実例 (2026-09-06 中山・芝 重): 10R カーヌスティ = ダート稍重3着・5着 vs 芝良1着 → 「道悪の方が悪い」-1.5。
12R アイムインディ = ダート稍重1着・重2着 (芝の道悪は未経験) → 「道悪の方が良い」+2.0。ダートの稍重/重は
芝の重とは質が異なる (脚抜きが良くなり時計が速い) ため、当日と同じ surface の走だけで比較すべき。

## 2. 変更仕様 (実装は jra-coder)
1. `score_weights.json` / `win5_weights.json` の `params.wet_aptitude` に `same_surface_only` (bool、既定 false=現行) を追加できるようにする。
   `eval_wet_aptitude` は `same_surface_only` が true のとき、`race_context["type"]` (芝/ダート) と同じ surface の走だけを wet/dry 集計に使う。
   surface 判定は `_run_course_info(run)` の第2要素 (芝/ダート) を使い、判定不能な走は除外。
2. **ML特徴 `wet_match` (`_ml_features`、学習パリティ) は変更しない**。`is_debut_horse`/`compute_score_ml` も不変。
3. `backtest_wet.py` に本番と同じ gap ルール (直近4走・道悪ベスト vs 良ベスト・gap>=2 → better/worse/neutral) を
   `--rule gap` として追加し、`--same-surface` で surface 分離を切替。出力: 道悪日 (一次) と良馬場日 (プラセボ) ×
   {better, worse, neutral} の n・勝率・複勝率・単回収 (win_pay/100n)、および芝日/ダート日の内訳。`--json` で outputs/t72/ に保存。
   既存の group 分類モード (`--rule group`、既定) の出力は不変。
4. テスト: `same_surface_only=false` で現行と同一結果 (回帰)、true でダート走が芝レースの集計から除外されること、
   `wet_match` 特徴が不変であること。

## 3. 検証設計 (事前凍結・結果閲覧前)
- データ: `ability.db` (封印 sha256 bbee39cc…990860a)。当日 condition が 稍/重/不 のレースのみ一次評価。
- 候補は2つだけ: mixed (現行) と same_surface。グリッド探索なし・閾値 (rank_gap=2, ±2.0/-1.5) 不変。
- 選抜: 2024年。固定テスト: 2025年 (選抜後に1回だけ実行・再調整禁止)。追加確認: 2026H1。
- 一次指標: 道悪日における「道悪の方が良い (better)」群の単勝回収率と「道悪の方が悪い (worse)」群の単勝回収率の差 (better − worse)。
  同じ定義で mixed と same_surface を比較する。
- 採用基準 (all_or_nothing):
  (a) 2024: same_surface の (better − worse) 差が mixed 以上、かつ better 群の複勝率が mixed 比 −1pt 以内。
  (b) 2024: プラセボ (良馬場日) の (better − worse) 差が same_surface で mixed より広がらない (+3pt 以内)。
  (c) 2025 固定テスト: (a) の差の符号が同じで、same_surface の差が mixed − 5pt 以上。
  (d) same_surface のシグナル該当頭数 (better+worse) が mixed の 40% 以上 (カバレッジ崩壊なし)。
  1つでも外れれば不採用 (現行維持) とし、負の結果として台帳と TASKS.md に記録する。
- 採用時の本番反映: 両 weights.json に `same_surface_only: true` を設定するのみ。ML/place モデルは不変。
