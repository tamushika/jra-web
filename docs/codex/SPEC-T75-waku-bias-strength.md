# SPEC-T75: 競馬場ごとの枠 (内・外) バイアス強度 (弱・中・強) 表示

起票: 2026-09-08 (Fable 5)。ユーザー要望: 「各競馬場のデータを見るときに、その競馬場で強烈な枠の内・外バイアスが働いているかを知りたい。弱・中・強で表現したい」。

## 1. 現状と問題
`api/past_data_service.get_track_bias_data(base_dir, place)` は直近開催日の **3着以内馬だけ** を人気差で加重した内枠/外枠スコア (`in_score`/`out_score`) を出し、比 1.3 倍で「イン有利/外枠有利/フラット」を出している。母数が「上位3頭×レース数」しかなく、期待値 (枠の頭数構成) と比べていないため、強さの目安にならない。

## 2. 設計: 全出走馬ベースの内外指数と z スコア
`races` テーブルは全着順の行 (rank, horse_number, total_horses, track_type) を持つので、直近開催日・surface ごとに:

1. 各出走馬を `calculate_waku(horse_number, total_horses)` で枠に変換し、**内 = 1〜3枠、中 = 4〜5枠、外 = 6〜8枠** に分ける (8頭以下のレースは枠=馬番なので同じ規則でよい)。障害レース (`race_name LIKE '%障%'`) と rank 非数値 (取消・中止) は除外。
2. 各馬の「3着以内になる期待値」 = `3 / total_horses` (そのレースの出走頭数)。グループごとに `expected = Σ 3/total_horses`、`observed = 3着以内の頭数`、`index = observed / expected` (1.0 = 期待どおり)。
3. z スコア: `z_in = (obs_in − exp_in) / sqrt(exp_in)`、`z_out` 同様。**bias_z = (z_in − z_out) / sqrt(2)** (正 = 内有利、負 = 外有利)。
4. 判定 (事前固定):
   - データ不足: surface のレース数 < 3、または内・外いずれかの出走頭数 < 10 → `level = "データ不足"`, `direction = "なし"`。
   - `|bias_z| < 1.0` → `level = "弱"` (実質フラット)、`direction = "なし"`。
   - `1.0 ≤ |bias_z| < 2.0` → `level = "中"`。
   - `|bias_z| ≥ 2.0` → `level = "強"`。
   - `direction` は bias_z > 0 なら "内"、< 0 なら "外" (弱のときは "なし")。
5. 返却に `waku_bias` を surface ごとに追加 (既存キーは不変):
   ```json
   "waku_bias": {
     "level": "強", "direction": "内", "z": 2.31, "races": 6,
     "in":  {"n": 28, "top3": 10, "expected": 5.6, "index": 1.79},
     "mid": {"n": 20, "top3": 4,  "expected": 4.0, "index": 1.00},
     "out": {"n": 26, "top3": 4,  "expected": 6.4, "index": 0.63},
     "label": "内枠バイアス 強 (内 1.79倍 / 外 0.63倍, 6R)"
   }
   ```
   `label` はUI表示用の1行文字列。データ不足のときは `"label": "枠バイアス: データ不足 (2R)"`。

## 3. 変更仕様
### 3.1 `api/past_data_service.py`
- 純関数 `compute_waku_bias(rows)` を追加。`rows` は `get_track_bias_data` が取得した当日全行 (dict の list、`rank`/`horse_number`/`total_horses`/`track_type`/`race_name`/`kaisai` を含む)。戻り値は `{"芝": {...}, "ダート": {...}}`。レース数は `(kaisai, race_name, distance)` の組で数える (kaisai に「Nレース」が無い SQLite 形式でも数えられるように)。
- `get_track_bias_data` の戻り値に `"waku_bias": compute_waku_bias(rows)` を追加。**既存の `evaluations`・`race_details`・`track_speed` は不変**。
- 例外時は既存どおり `{"error": ...}`。`compute_waku_bias` 内の例外は surface ごとに `{"level": "データ不足", ...}` に倒し、全体を落とさない。

### 3.2 `script.js` `renderTrackBias`
- 各 surface パネルの「枠番傾向（加重スコア）」ブロックの直後に、`data.waku_bias[key]` があれば1行のバッジを追加:
  `<div class="tb-waku-bias"><span class="tb-verdict {cls}">枠バイアス: {direction}{level}</span> <span class="tb-speed-diff">内 {in.index}倍 / 外 {out.index}倍 (3着内/期待, {races}R)</span></div>`
  `cls`: 強 → `strong` (赤系 `#ff3366`)、中 → `medium` (橙 `#fb923c`)、弱・データ不足 → `flat`。`direction` が "なし" なら「枠バイアス: 弱 (フラット)」。
- `style.css` に `.tb-verdict.strong` / `.tb-verdict.medium` を追加 (既存 `.tb-verdict.inner/.outer/.flat` に倣う)。
- 本番Web (`/`) と統合版 `/race/` の両方で表示される (同じファイル)。

## 4. テスト (`tests/test_t75_waku_bias.py`)
- `compute_waku_bias` の単体: (a) 内枠に3着内が集中する合成データ (6レース×16頭、内枠から各レース3頭中2〜3頭) → `direction="内"`, `level in ("中","強")`, `z>0`。(b) 一様 (各レースの3着内を枠均等に) → `level="弱"`。(c) 2レースのみ → `データ不足`。(d) 8頭立てのみ (枠=馬番) でも動く。(e) 障害・rank 非数値の行が除外される。
- `get_track_bias_data` を SQLite の一時DB (`races` テーブルを作成し数レース分 INSERT) + `get_db_connection` monkeypatch で通し、戻り値に `waku_bias` と既存キーが両方あること。
- 既存テスト (`tests/` 全体) が通ること。

## 5. 受け入れ (手動)
- 解析画面の「直近実績（トラックバイアス）解析」で、芝・ダートそれぞれに「枠バイアス: 内 強」等のバッジと倍率が出る。
