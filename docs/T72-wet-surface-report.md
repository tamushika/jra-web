# T72 道悪適性の芝/ダート分離 (same_surface_only) 検証報告

実施: 2026-09-07 (Fable 5)。台帳: `T72-wet-aptitude-same-surface-v1` (登録→result→adjudication)。SPEC: [SPEC-T72](codex/SPEC-T72-wet-aptitude-surface.md)。

## 結論: 不採用 (負の結果)。本番は現行 (mixed) 維持

## 2024 選抜 (ability.db、道悪日=当日 稍/重/不、gapルール rank_gap=2)

| 候補 | better n | better 単回収 | better 複勝率 | worse n | worse 単回収 | better−worse | プラセボ差 (良馬場日) |
|---|---|---|---|---|---|---|---|
| mixed (現行) | 1472 | 83.0% | 19.4% | 2522 | 60.2% | **22.8pt** | -3.3pt |
| same_surface | 1220 | 71.7% | 19.8% | 2028 | 58.7% | **13.0pt** | -17.0pt |

surface 内訳 (道悪日 better/worse 単回収): 芝 mixed 67.5%/45.5% → same 71.6%/42.9%、ダート mixed 89.8%/69.0% → same 71.8%/67.8%。

## 事前基準の判定
- (a) 一次指標 same ≥ mixed: **不通過** (13.0 < 22.8)。better 複勝率は −1pt 以内で可
- (b) プラセボ差が広がらない: 通過
- (c) 2025 固定テスト: stop_rule により未実行
- (d) カバレッジ ≥40%: 通過 (81%)

## 解釈
芝の道悪日に限れば同surface限定は僅かに良いが、ダートの道悪日では芝の道悪走を含めた現行の方が「道悪の方が良い」群の回収を大きく上回る。
直感 (ダート稍重≠芝重) に反して、混合の方が予測情報を持つ。芝限定の非対称適用は n が少なく、再挑戦するなら prospective 専用の新登録が必要。

## 成果物
- `api/scoring.py`: `params.wet_aptitude.same_surface_only` (既定 false=現行)。ML特徴 `wet_match` は不変
- `backtest_wet.py`: `--rule gap` / `--same-surface` / `--json`
- `outputs/t72/sel2024_*.json`、`tests/test_t72_wet_surface.py` (10件)
