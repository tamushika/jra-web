# T54a 事前脚質・展開圧力レポート

実行日: 2026-07-23  
状態: Stage 0およびT39登録後の固定評価完了（採否は上位モデル）

## 固定した特徴定義

1. `style_pos_asof`: 対象日より前の直近4走を先に取り、その中の有効な `c4 / total_horses` を平均
2. `race_pace_pressure_asof`: 同一レース全馬の `style_pos_asof` の平均（自馬を含む）
3. `style_pace_interaction`: 上記2値の積
4. `style_available`: 直近4走中に有効なc4が2走以上なら1、それ以外は0

有効履歴が2走未満の場合は `style_pos_asof=0`、`style_pace_interaction=0` とする。同日レースと未来レースは履歴から日付単位で除外する。既存21特徴、本番artifact、表示・通知経路は変更していない。

## Stage 0健全性診断（2024選抜期間）

| 指標 | 値 |
|---|---:|
| 対象出走 | 46,752 |
| 脚質利用可能 | 37,261 |
| 利用可能率 | 79.6993% |
| 相関対象 | 37,261 |
| `style_pos_asof` と当該レース実現c4比のPearson相関 | 0.462004 |

特徴要約:

| 特徴 | 平均 | 標準偏差 |
|---|---:|---:|
| `style_pos_asof` | 0.377922 | 0.263582 |
| `race_pace_pressure_asof` | 0.377922 | 0.152532 |
| `style_pace_interaction` | 0.166091 | 0.126840 |
| `style_available` | 0.796993 | 0.402238 |

この相関は診断用副指標であり、候補選抜コードには入力されない。相関値によるゲート判定や特徴構成変更は行っていない。

## 評価ハーネス

- baseline 21特徴 × L2 `{0.3, 1, 3}` と、固定4列追加 × 同L2の計6候補
- 学習2021～2023、一次選抜2024、2021～2024 final-refit後に2025・2026H1を固定評価
- 一次指標は2024勝馬LogLoss
- Brier、top-k、市場比較、Harville由来top3 Bernoulli LogLoss/Brier、WIN5開催日paired bootstrapを副指標として出力
- 距離帯、および展開圧力帯×勝馬脚質帯のtop3 LogLoss増分を診断出力
- パックOFF時は基礎21特徴行列を同一オブジェクトで返し、byte SHA一致を評価前にassert
- `T54a-running-style-pace-v1` のT39 base登録とability.db封印SHA一致がなければ、特徴量構築・6候補評価より前に停止

## 現在の停止点

Stage 0結果JSONは `outputs/t54a_stage0.json`（SHA-256: `8a8fa6ba0e7fdbc12cfc672460860d126d65ec8d07bf95445bb47436d3409a04`）。

評価コマンドの未登録停止を確認済み:

```text
T54a evaluation blocked: register T54a-running-style-pace-v1 in T39 ledger first
```

上位モデルによるT39登録後にのみ6候補評価を実行し、本レポートへ数値を追記する。Stage 0時点では解釈・採否を行わない。

## 上位モデルレビュー追補 (2026-07-23, Fable 5)

**受理・独立検証済み**: Stage 0 JSON全数値・ability.dbハッシュを報告書と突合し完全一致を
確認。7テスト+全体529テストパス。`backtest_t54a.py evaluate` を自分でも再実行し、
`T54a evaluation blocked: register T54a-running-style-pace-v1 in T39 ledger first` で
確実にブロックされ、`outputs/t54a_result.json` が生成されないことを独立確認した。

**設計上の観察 (ブロッカーではない)**: `race_pace_pressure_asof` はレース内の
`style_available=0`の馬 (履歴不足) についてもフォールバック値0を平均に算入する
(`test_pressure_includes_each_runner_and_uses_fallback_zero` で意図的にテスト・
文書化された挙動)。この結果、新馬・履歴薄の馬が多いレース (典型的には未勝利戦等)
ほど展開圧力が「先行寄り」に偏って見える可能性がある。これは明示的な誤りではなく
妥当な単純化だが、**6候補評価の距離帯別/脚質帯別内訳を見る際、この偏りとレース
クラスの交絡を念頭に置いて解釈する**。挙動自体を修正する必要はない — 最終的な
LogLossがこの単純化の影響を含めて判断してくれる設計のため。

## 固定評価結果（2026-07-23）

T39登録とability.db封印SHA一致を確認後、6候補を固定評価した。

### 2024一次選抜

| 構成 | L2 | 勝馬LogLoss | Brier | top1 / top2 / top3 |
|---|---:|---:|---:|---:|
| baseline | 0.3 | **1.954704** | 0.057413 | 32.76% / 51.88% / 64.90% |
| style4 | 0.3 | 1.954749 | **0.057412** | 32.76% / 51.27% / 64.80% |
| 市場 | — | 1.965945 | 0.057734 | 32.86% / 51.27% / 64.29% |

一次指標ではbaselineが選抜された。診断用style4−baseline開催日paired差は
`+0.000045`、95% CI `[-0.000500, +0.000597]`、p=`0.8976`
（983R、106日ブロック）。

Harville由来top3 LogLossはbaseline `0.435660`、style4 `0.435674`。
距離帯別差は1600m未満 `-0.000053`、1600–1999m `-0.000078`、
2000m以上 `+0.000284` で、安定した改善帯は確認されなかった。

### style4固定診断のHistorical benchmark

| 期間 | style4 LogLoss | baseline LogLoss | 差 | paired p |
|---|---:|---:|---:|---:|
| 2025 | **2.037265** | 2.037371 | -0.000106 | 0.7676 |
| 2026H1 | 2.037523 | **2.036348** | +0.001175 | 0.0150 |

2025は非有意の微改善、2026H1は有意な悪化へ反転した。2026H1のtop3 LogLossも
style4 `0.425570`、baseline `0.425537` と悪化方向。展開圧力×勝馬脚質帯の内訳も
期間をまたいだ一貫性はなかった。

結果JSON: `outputs/t54a_result.json`（SHA-256:
`44bd5023840343d48f616e6ec7bf33b89d00b1132138d82dad0808d4ed2896b2`）。
数値の解釈・採否、台帳result/adjudication追記は上位モデルが行う。
