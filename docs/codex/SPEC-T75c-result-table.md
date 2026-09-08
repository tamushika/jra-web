# SPEC-T75c: トラックバイアス欄に実データの結果表 (1〜3着の馬番・人気・枠・脚質、決着パターン) を追加

起票: 2026-09-09 (Fable 5)。ユーザー要望: 「弱中強だけでなく実データの結果も表で欲しい。何番人気の何番が1〜3着だったのかを分かりやすく。逃げ決着か差し決着かも見たい」。

## 1. 設計
- T75b で集計対象にした直近14日以内の全開催日・全レース (障害除外) について、1〜3着馬の情報を `result_table` として API に追加し、解析画面の「詳細表示」で日付・レースごとの表として描画する。
- 脚質は `corner_4` (4角位置) から判定: 1 = 逃げ、2〜4 = 先行、5〜9 = 差し、10以上 = 追込、不明 = "?"。
- 決着パターン (レース単位):
  - 脚質: 1〜3着の4角位置が全て ≤4 → 「前残り」、全て ≥5 → 「差し決着」、1着が ≤4 で他に ≥5 あり → 「先行勝ち・差し届く」、1着が ≥5 → 「差し勝ち」。不明が含まれれば判定できる馬だけで決め、全て不明なら "?"。
  - 枠: 1〜3着の枠グループ (内=1〜3枠、中=4〜5枠、外=6〜8枠、T75 と同じ) が全て内 → 「内決着」、全て外 → 「外決着」、内2頭以上 → 「内寄り」、外2頭以上 → 「外寄り」、それ以外 → 「混合」。

## 2. 変更仕様
### 2.1 `api/past_data_service.py`
1. 純関数 `build_result_table(rows, latest_date=None, window_days=14, half_life_days=7.0)` を追加。`rows` は T75b の複数日 rows (date 列付き)。戻り値は surface ごとのリスト:
   ```json
   "result_table": {"芝": [
     {"date": "260906", "weight": 1.0, "race_num": 10, "race_name": "HTB賞", "distance": 1800,
      "condition": "良", "total_horses": 14, "win_odds": "260",
      "top3": [
        {"rank": 1, "num": 9, "waku": 5, "group": "中", "pop": 1, "c4": 3, "kyaku": "先行", "name": "…", "jockey": "吉田 隼人"},
        {"rank": 2, "num": 8, "waku": 4, "group": "中", "pop": 5, "c4": 5, "kyaku": "差し", ...},
        {"rank": 3, ...}],
      "finish_kyaku": "先行勝ち・差し届く", "finish_waku": "混合"}
   ], "ダート": [...]}
   ```
   - `race_num` は `race_num` 列があればそれ、無ければ `kaisai` の「Nレース」から抽出。レースの識別は T75b と同じ `(kaisai, race_name, distance)`。
   - 並び: date 降順 → race_num 昇順。`top3` は rank 昇順 (同着があれば両方入れる)。`name` は `馬名` 列 (無ければ None)、`win_odds` は 1着行の `odds` (単勝配当、無ければ None)。
   - rank 非数値・障害は除外。`weight` は T75b と同じ式 (表示用)。
2. `get_track_bias_data` の戻り値に `"result_table": build_result_table(multiday_rows, latest_date=latest_date)` を追加。既存キーは不変。

### 2.2 `script.js` `toggleBiasDetail()` / `renderTrackBias()`
1. 「詳細表示 ▼」を押したときの内容を次の順に変更:
   a. **結果表** (surface ごと): 見出し「🌿 芝 — 直近N日の結果 (1〜3着)」。列: `日付` (M/D、重み ×w を小さく) | `R` | `レース名 (距離)` | `馬場` | `1着` | `2着` | `3着` | `脚質決着` | `枠決着`。
      各着セルは `9番 <b>1人気</b> 5枠 先行` の形式。枠グループで色分け (内=青系 `.tbd-in`、外=赤系 `.tbd-out`、中=無色)。人気は 1〜3人気を太字、6人気以下を橙 (`.tbd-pop-hi`) にして荒れが見えるようにする。1着セルには単勝配当があれば `(260円)` を付ける。
      `脚質決着` セルは「前残り」「差し決着」を色分け (`.tbd-front` 緑系 / `.tbd-closer` 紫系)、`枠決着` は内決着/内寄り=青系、外決着/外寄り=赤系。
   b. その下に折りたたみ「加重スコア内訳を表示 ▼」 (既定は閉じる) を置き、**従来の加重スコア内訳テーブルをそのまま**中に入れる (削除しない)。
2. `result_table` が無い/空の場合は結果表を出さず、従来どおり加重スコア内訳だけ表示。
3. `detailBtn` の表示条件は `race_details` または `result_table` のどちらかがあること。
4. `style.css` に上記クラス (`.tbd-in`, `.tbd-out`, `.tbd-pop-hi`, `.tbd-front`, `.tbd-closer`, `.tbd-sub-toggle`) を追加。既存クラスは変更しない。

## 3. テスト (`tests/test_t75_waku_bias.py` に追加)
- `build_result_table`: 2日×2レースの合成データで、date 降順・race_num 昇順、`top3` が rank 順、脚質ラベル (c4=1→逃げ, 3→先行, 6→差し, 12→追込)、`finish_kyaku` (全≤4→前残り、全≥5→差し決着、1着≤4+他≥5→先行勝ち・差し届く、1着≥5→差し勝ち)、`finish_waku` (全内→内決着、内2頭→内寄り、混合) を確認。
- 一時 SQLite での `get_track_bias_data` 通しに `result_table` の存在確認を追加。
- 既存テスト全体が通ること。`node --check script.js`。
