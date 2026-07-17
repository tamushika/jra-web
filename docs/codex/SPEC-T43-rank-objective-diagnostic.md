# SPEC T43: 順位学習診断 (Plackett–Luce top-1 / @3 / @5 / full の比較)

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T43。
背景: [RE-accuracy-ideas-20260717.md](RE-accuracy-ideas-20260717.md) §6 (採用済み)。
起票: 2026-07-18 上位モデル。

**位置づけ: 期待効果は小〜不明の安価な診断** (現行CLは既にPL top-1尤度であり、
全順位化しても本番推論 `softmax(w·x)` の表現力は増えない。変わるのは係数推定に
2着以下を使う点のみ)。**実装者は数値報告まで。採否・台帳登録・コミットは上位モデル。**

## 1. 実装方式 (隔離)

- 新スクリプト `backtest_rank_objective.py`。**本番共通関数
  (`fit_conditional_logit`・`win_probs_from_ml_scores`・本番artifact) は変更しない**
- T33のas-ofデータ構築 (`build_dataset` + ability as-of統計) を再利用
- 学習目的4種だけを比較: ①現行top-1 ②truncated PL@3 ③truncated PL@5 ④full PL
- 勾配実装は数値勾配との一致テスト必須 (RE §6.5)

## 2. 実装上の落とし穴への対処 (RE §6.3 — 全て必須)

1. **「全順位」= 特徴を構築できた馬だけの順位**である事実を明示し、
   完全フィールド (全馬特徴あり) 限定の感度分析と除外件数を報告
2. **同着・中止・失格・取消を含むレースは除外**して件数報告 (tie-aware PLは別タスク)
3. **レースweight正規化**: full PLは1レースから頭数−1個のchoice eventを作るため、
  各レースのstage weight合計を1に正規化 (大頭数レースの過重み防止)
4. **L2は現行値の流用禁止**: 固定小グリッドから2021-24内で選ぶ
5. **一次指標は勝馬側**: 勝馬Log Loss。全順位NLL/Spearmanは副指標
6. **確定オッズ学習と購入時点推論の乖離**: `ln_odds` 係数が目的間でどう動くかを
   報告 (共通cutoff評価の代替として係数比較を併記)

## 3. 評価 (T39プロトコル準拠・実行前に上位モデルが台帳登録)

- 2021-24内のrolling-origin (年次fold) でtop-m・L2を選抜し固定
- 2025・2026H1は固定モデルのhistorical benchmark報告のみ
- 指標: 勝馬Log Loss (一次)・Brier・top-1/2/3・市場フロア・WIN5開催日paired
- グリッド (台帳登録時に確定): 目的4種 × L2 ∈ {0.3, 1, 3} = 12候補

## 4. 採用時の本番接続 (本診断ではやらない — 参考規定)

採用となった場合も `objective: "conditional_logit"` は不変とし、
`training_objective: "plackett_luce_top3_v1"` 等のメタデータ分離で後方互換を保つ
(RE §6.4)。本診断の段階で本番loaderに触れる必要はない。

## 5. テスト

数値勾配一致 (4目的×小fixture)、同着除外、weight正規化 (頭数違いのレースで
stage合計1)、決定論SHA、tests/ 全体パス。
