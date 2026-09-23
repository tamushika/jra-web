# T80b: waku 修正後 CL 再学習と凍結分割ゲート

- ability.db sha256 (実行前): `bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a`
- ability.db sha256 (実行後): `bbee39cc91d7c42b9df6f67bd58875514c5eda7977adb165e330c11ab990860a`
- production model sha256: `8687f9bfa2278ed1dcafd9f13c90b08fa6b6d58f993a2c139fd39c9edbf34527`
- candidate model sha256 (2回一致): `a0efd7e96cfb59bf92d1c7437f98357d1ca5fb3b2dbecc106a7027d045730e28` (match=True)
- factor snapshot sha256: `4764d44a7a6304670ffc03bed5801b73a45a62e8707184cf2d144e1f0f358775`
- rows: 全233525行中 f_pts が legacy/fixed で異なる行: 123677 (53.0%)

## Origin × 母集団 × {legacy, fixed, market, production} の LL / top-k

| origin | population | legacy_ll | fixed_ll | market_ll | production_ll | fixed-legacy (95%CI) | topk legacy | topk fixed | topk market |
|---|---|---|---|---|---|---|---|---|---|
| A_2024 (20240101-20241231) | all_races | 1.873836 | 1.874035 | 1.880292 | 1.871728 | +0.000199 [-0.000234, +0.000648] | 34.30, 53.56, 68.23, 77.45 | 34.26, 53.48, 68.02, 77.45 | 34.22, 53.31, 67.47, 76.82 |
| A_2024 (20240101-20241231) | win5 | 1.954680 | 1.955130 | 1.965945 | 1.952333 | +0.000450 [-0.000267, +0.001199] | 32.76, 51.78, 64.80, 73.45 | 32.55, 51.48, 64.50, 73.45 | 32.86, 51.27, 64.29, 72.53 |
| B_2025 (20250101-20251231) | all_races | 1.923072 | 1.923025 | 1.925172 | 1.920889 | -0.000047 [-0.000193, +0.000089] | 33.02, 53.29, 66.43, 75.20 | 33.11, 53.20, 66.47, 75.12 | 33.19, 53.75, 66.98, 75.03 |
| B_2025 (20250101-20251231) | win5 | 2.037293 | 2.037185 | 2.039187 | 2.034621 | -0.000108 [-0.000342, +0.000117] | 30.09, 49.75, 62.09, 71.11 | 30.29, 49.75, 61.99, 71.11 | 30.39, 49.55, 62.39, 70.71 |
| C_2026H1 (20260101-20260630) | all_races | 1.953434 | 1.953655 | 1.956248 | 1.953688 | +0.000221 [-0.000343, +0.000798] | 32.98, 50.44, 63.23, 72.31 | 33.25, 50.53, 63.32, 72.40 | 32.72, 50.44, 63.32, 72.49 |
| C_2026H1 (20260101-20260630) | win5 | 2.035581 | 2.035665 | 2.040464 | 2.035711 | +0.000084 [-0.000909, +0.001072] | 29.92, 47.64, 59.84, 69.29 | 30.31, 47.83, 60.04, 69.49 | 30.71, 47.44, 59.06, 68.70 |

- races/horses per origin×population: see `outputs/t80b/report.json`.

## ゲート判定 (機械適用)

`gate_pass = True`

```json
{
  "a_ll_all_races": {
    "checks": {
      "2025": {
        "fixed_ll": 1.923025,
        "legacy_ll": 1.923072,
        "pass": true
      },
      "2026H1": {
        "fixed_ll": 1.953655,
        "legacy_ll": 1.953434,
        "pass": true
      }
    },
    "pass": true
  },
  "b_topk_floor_win5": {
    "checks": {
      "2025": {
        "delta_pt_k1_3": [
          0.200602,
          0.0,
          -0.100301
        ],
        "pass": true
      },
      "2026H1": {
        "delta_pt_k1_3": [
          0.393701,
          0.19685,
          0.19685
        ],
        "pass": true
      }
    },
    "pass": true
  },
  "c_win5_sign_consistent": {
    "checks": {
      "2025": {
        "diff": -0.000108,
        "sign": 0
      },
      "2026H1": {
        "diff": 8.4e-05,
        "sign": 0
      }
    },
    "pass": true
  },
  "d_production_2026h1": {
    "production_ll": 1.953688,
    "fixed_ll": 1.953655,
    "pass": true
  },
  "gate_pass": true
}
```

stop_rule_would_trigger (2025, all_races, informational only): `False`

## 実行情報

- 実行時間: 145.8秒
- テスト: pytest tests/test_t80b_waku_retrain.py

採否の記述はしない (上位モデルが裁定)。本ファイルは SPEC-T80b §2-5 の報告物であり、gate_pass は機械適用結果に過ぎない。

