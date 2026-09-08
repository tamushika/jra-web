# SPEC-T75b: 枠バイアス強度を直近複数開催日 (近い日ほど重視) で判定する (T75 追補)

起票: 2026-09-08 (Fable 5)。ユーザー要望: 「直近だけでなく先週までの開催を加味。9/6 (日) なら 8/29・8/30・9/5 を含めて判定。ただし近い開催ほど重視」。

## 1. 設計
- 対象日: その競馬場の最新開催日 D を含む **直近14日以内** (D−13 〜 D) の全開催日 (通常 3〜4 日。開幕週は 1〜2 日)。
- 重み: 半減期 7 日の指数減衰 `w = 0.5 ** (Δdays / 7)` (Δdays = D − その日)。例 D=9/6: 9/6=1.00、9/5=0.91、8/30=0.50、8/29=0.46。
- 集計 (surface ごと、T75 の内/中/外 区分と除外規則はそのまま):
  - `obs_w = Σ w·1[3着以内]`、`exp_w = Σ w·(3/頭数)`、`var_w = Σ w²·(3/頭数)`
  - `index = obs_w / exp_w`、`z_grp = (obs_w − exp_w) / sqrt(var_w)`、`bias_z = (z_in − z_out) / sqrt(2)`
  - 判定閾値 (|z| 1.0 / 2.0 → 弱/中/強) と direction は T75 と同じ。
  - データ不足: **重みなしの** レース数 < 3、または内/外の出走頭数 (重みなし) < 10。
- 既存の `evaluations` (最新日のみの加重スコア)・`race_details`・`track_speed`・`latest_date` は不変。

## 2. 変更仕様
### 2.1 `api/past_data_service.py`
1. `get_track_bias_data` で最新日 D を求めた後、`SELECT DISTINCT date FROM races WHERE place=? AND date<=? ORDER BY date DESC` から D−13 以内の日付を集める (`YYMMDD` を datetime に変換して日数差を計算)。
2. それらの日付の行を `date` 列付きで取得する (既存の当日クエリはそのまま残し、追加クエリで多日分を取る。または既存クエリを `date IN (...)` にして `date` 列を含め、当日分だけを既存処理に渡す。どちらでもよいが **既存の当日処理に渡す rows の内容は変えない**)。
3. `compute_waku_bias(rows, latest_date=None, half_life_days=7.0, window_days=14)` に拡張: `rows` の各行に `date` があれば重みを付け、無ければ (旧呼び出し・テスト互換) 重み 1.0。戻り値の surface ごとに以下を追加:
   ```json
   "days": [{"date": "260906", "weight": 1.0, "races": 6, "in_index": 1.85, "out_index": 0.55},
            {"date": "260905", "weight": 0.91, "races": 6, "in_index": 1.1, "out_index": 0.9}, ...],
   "window_days": 14, "half_life_days": 7
   ```
   `label` は `内枠バイアス 強 (内 1.62倍 / 外 0.71倍, 4日 22R)` の形式に変更 (日数とレース数)。
4. `in`/`mid`/`out` の `n`/`top3` は重みなしの実数、`expected`/`index` は重み付き (index = obs_w/exp_w)。

### 2.2 `script.js` `renderTrackBias`
- バッジ横の説明を `内 1.62倍 / 外 0.71倍 (3着内/期待, 4日 22R・近い日ほど重視)` にし、`title` 属性 (ツールチップ) に日別の内訳 `9/6 ×1.00: 内1.85/外0.55 (6R)` … を改行区切りで入れる。日付は `YYMMDD` → `M/D` 表記。
- それ以外の表示は不変。

## 3. テスト (`tests/test_t75_waku_bias.py` に追加)
- 3日分の合成データ: 最新日は内枠集中、10日前は外枠集中で頭数同じ → 重み付きでは内が優勢 (direction=内)、重みを全て 1.0 にした場合 (`half_life_days` を極大にする) より |z| が異なることを確認。
- `date` 無しの行だけを渡した旧形式の呼び出しが従来どおり動く (重み 1.0)。
- 一時 SQLite に 3 開催日 (D, D−1, D−8) を入れて `get_track_bias_data` を通し、`waku_bias[surface]["days"]` が 3 件・重みが降順、`evaluations` は最新日のみで計算されている (最新日の 3着内数と一致) こと。
- 既存テスト全体が通ること。

## 4. 受け入れ (手動)
- 阪神を解析すると「内枠バイアス 強 (…, 4日 24R・近い日ほど重視)」のように複数日で判定され、ツールチップに日別内訳が出る。
