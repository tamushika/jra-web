# SPEC-T70b: 仮想運用の追加パターン P5/P3 (推定専用)

作成: 2026-08-20 (Fable 5)。実装: jra-coder。レビュー・台帳登録・コミット: 上位モデル。
根拠: [T70パターン討議](../T70-pattern-discussion.md) (Fable 5×Codex、CONVERGED)。
この討議記録が仕様の正 — 本SPECは実装詳細のみ規定する。

## 1. 共通事項

- P1 (`v1`) の挙動・記録・登録は**一切変更しない** (凍結済みconfirmatory primary)
- P5/P3は **estimation-only** (実マネー移行資格なし)。表示にもその旨を明示
- 各パターンは独立財布 (本体stake日上限¥10,000/パターン)。冪等キーは既存形式
  (`{policy_version}:{race_id}:{bet_type}:{suffix}`) で自然に分離される
- 決定は既存フック (jra_ev.py の T62bスナップショット書込直後) 1箇所のまま、
  `virtual_betting.record_decision` 内で全パターンを評価する。**jra_ev.pyの追加変更は
  不可** (フック呼び出しはそのまま。通知関数3つのAST不変検証は継続)
- as-of保証・精算・返還はP1と同一機構を再利用

## 2. P5 (全適格ベースライン、policy_version="p5-v1")

- 対象: `race_confidence_snapshots` の **selected値を問わず** status=ok相当の行
  (model_probs_json/market_odds_json あり・凍結manifest SHA一致)。9R未満等の
  failure行は従来どおり対象外
- 本体: CL勝率1位馬 (P1と同じ選択関数) の複勝 **¥1,000**
- 対照: 同レース1番人気 (cutoffオッズ最小) の複勝 **¥1,000**・is_control=1・予算外
- 日上限¥10,000 (本体stakeのみ算入)。cutoff到着順に消化し超過は skipped_budget
- 注: T62b選定レースはP1とP5の両方が記録する (財布が独立なので二重ではなく設計どおり)

## 3. P3 (λ複勝EV、policy_version="p3-v1")

- 対象: P5と同じ全適格行のうち、**同一レースの30分前 board_odds_snapshots
  (bet_type='fukusho') が取得できるもの**。結合規約: 当該レースの
  `observed_at <= cutoff_at` の最新スナップショット (T62b cutoffと同一ループ由来)。
  無ければベットせず **status='skipped_data'** で1行記録 (でっち上げ禁止)
- 複勝確率: cutoffの単勝オッズ由来市場勝率 → **T59d本番と同一の導出関数**
  (λ2=0.8301849569 固定・7頭以下は対象外=skipped_data) で馬別複勝確率を計算。
  独自実装せず既存モジュールを呼ぶこと
- 本体: `odds_low × 複勝確率` が最大の1頭、**その値が1.00以上のときのみ** 複勝¥1,000
  (1.00ちょうどは購入)。同値タイブレークは馬番小。1レース最大1頭
- 1.00未満の日・レースは何も買わない (ベット0の日は正常)。対照なし
- 日上限¥10,000・発走時刻順・超過は skipped_budget

## 4. スキーマ

- `virtual_bets.status` に **'skipped_data' を追加** (SQLiteのCHECK制約は変更不能の
  ため、migration v13で新テーブル作成→全行コピー→rename の再構築。既存行・既存
  CHECK意味論は不変。skipped_dataはpayout=NULL側のCHECKに含める)
- 他のスキーマ変更禁止

## 5. 表示 (perfダッシュボード)

- 仮想運用セクションを**パターン別ブロック** (P1/P5/P3) に拡張。各ブロックに
  位置づけラベル (P1=「移行ゲートあり」/ P5・P3=「推定専用 (実運用移行資格なし)」)
  と既存disclaimerを表示
- P3ブロックには **λ校正の実測乖離** (settledベットの Σ予測複勝確率 vs 実現複勝率)
  を併記
- 禁止語彙テスト・「仮想運用であり実購入ではない」明示は既存方式を踏襲

## 6. テスト

1. ポリシー分離: P5/P3追加後もP1の決定行が既存テストと完全一致 (冪等キー衝突なし)
2. P5: 非選定レースで記録される/選定レースでP1とP5両方記録される/上限・対照
3. P3: 閾値1.00境界 (ちょうど=購入、未満=なし)/board未取得=skipped_data/
   7頭以下=skipped_data/タイブレーク
4. migration v13: 既存行保全・新status受理・旧status意味論不変
5. AST: jra_ev.py通知関数3つ不変 (既存テストの継続パス)
6. 全体: `python -m pytest tests/ -q` 全パス

## 7. 完了条件

- テスト全パス+実DBドライラン (8/15・8/16をP5/P3で「もし稼働していたら」) を報告
- コミットはしない (上位モデルがレビュー後、p5-v1/p3-v1 をestimation-onlyとして
  台帳登録してからコミット・デプロイ)
