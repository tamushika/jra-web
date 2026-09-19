# SPEC-T80: `past_data_service.calculate_waku` の枠割当を JRA の規則に修正し、影響範囲を再計測する

起票: 2026-09-19 (Fable 5.1)。T79 レビュー中に発見。

## 0. 事実
- JRA の枠番割当: 頭数 h ≤ 8 なら 枠=馬番。h > 8 なら 各枠 `h // 8` 頭を基本とし、余り `h % 8` 頭を **外枠から 1 頭ずつ** 追加 (16頭=全枠2頭、18頭=1〜6枠2頭・7〜8枠3頭、12頭=1〜4枠1頭・5〜8枠2頭、10頭=1〜6枠1頭・7〜8枠2頭)。
- 本番の出馬表解析 `api/index.py:calculate_waku` と `jra_lite.py` は **この規則どおり** (`q, r = divmod(total, 8); counts = [q + (1 if i > 8 - r else 0) ...]`)。`fold_stats._calculate_waku` (T14 統計) も正しい。
- **`api/past_data_service.py:calculate_waku` は誤り**: 余りを外枠に **2 頭ずつ** 足すため、16頭で `[1,1,1,1,3,3,3,3]`、18頭で `[1,1,1,3,3,3,3,3]`、12頭で `[1,1,1,1,1,1,3,3]`、10頭で `[1,1,1,1,1,1,1,3]` になる (2026-09-19 実測)。
- 誤った関数の利用箇所 (= 影響範囲):
  1. `past_data_service.analyze_races` (過去データ分析タブの枠別成績)
  2. `_compute_waku_bias_one` / `build_result_table` / 1009 行付近 (**T75/T75b/T75c 枠バイアス強度・結果表の内/中/外**) — 内1〜3枠・中4〜5枠・外6〜8枠の判定が 9 頭以上で系統的にずれる (16頭なら馬番4〜7が「中」、8〜10が「外」扱いになるべきところ、実際は馬番4〜5=中、6以降=外 … の逆)。**2026-09-06 の「阪神芝=内枠バイアス強 (z=2.3)」は再計測が必要**
  3. `backtest_ml.py:643` (CL モデル学習時の frame 因子ポイント)、`backtest_criteria.py:92` (採掘ルールの `w_num`)、`backtest_score.py`、`backtest_win5.py`、`tests/test_feature_parity.py` — 学習/採掘は誤った枠、本番 (index.py) は正しい枠で評価しており **学習と本番で枠特徴がずれている** (9 頭以上のレース)
  4. `build_graded_cache.py` (T79、修正後に再生成するだけでよい)
- `tests/test_t75_waku_bias.py` のフィクスチャ注釈は誤った対応 (`5〜7→5枠, 8〜10→6枠…`) を前提に書かれている。

## 1. 変更
1. `api/past_data_service.py:calculate_waku(umaban, head_count)` を JRA 規則に修正する。実装は `api/index.py:calculate_waku` と同じ結果になること (境界: umaban ≤ 0 / head_count ≤ 0 / 非数 / umaban > head_count は None のまま)。
2. `tests/test_t75_waku_bias.py`: フィクスチャ注釈と期待値を正しい割当で書き直す (16頭: 1〜2→1枠 … 15〜16→8枠、内=馬番1〜6、中=7〜10、外=11〜16)。シナリオの意図 (内寄り集中 / フラット) は保つ。
3. 新規 `tests/test_t80_waku_allocation.py`: 9/10/12/14/16/17/18 頭で `past_data_service.calculate_waku` と `index.calculate_waku` と `fold_stats._calculate_waku` の 3 実装が全馬番で一致することを検証 (パラメトライズ)。
4. 既存テスト全体 (`tests/`) が通ること (`test_feature_parity` は本番と同じ関数を使うようになるので通るはず)。

## 2. 再計測 (コードは変えない・数値を報告するだけ。採否判断は上位モデル)
1. **T75 枠バイアス強度**: `past_data_service.get_track_bias_data(base_dir, place)` を修正前後で実行し、直近開催 (中山・阪神・札幌など取得できる場) の `waku_bias` (in/out の倍率, z, 強度ラベル) を **before/after の表** で報告する。修正前は `git stash` を使わず、修正前の関数を一時的に別名でコピーして比較すること (作業ツリーには他の未コミット変更があるため)。
2. **学習特徴のずれ**: `backtest_ml.py` の学習行のうち `total_horses > 8` の割合と、修正前後で `calculate_waku` の値が変わる (umaban, total_horses) の組合せの割合を、ability.db の 2021〜2025 `runs` から集計して報告する (再学習はしない)。
3. `mined_rules_v2.csv` のうち条件に「枠番」を含むルール数を報告する。

## 3. 報告に含めること
- 変更ファイル、テスト結果 (全体件数)、§2 の表と数値。
- **ability.db は読み取り専用。稼働中の統合サーバー (5005) は再起動しない。`git checkout` / `git stash` 禁止 (作業ツリーに他作業者の未コミット変更あり)。`docs/TASKS.md` は触らない。コミットしない。**

## 4. 上位モデルの後続判断 (このタスクの範囲外)
- 再学習 (T45 系) と再採掘 (T15 系) を修正後の枠で実施するか、凍結分割でのゲート判定。
- T75 の判定文言 (2026-09-06 の阪神芝) の訂正。
