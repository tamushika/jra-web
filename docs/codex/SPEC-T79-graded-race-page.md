# SPEC-T79: 重賞データページ (過去傾向・データの一覧) を統合版に追加する

起票: 2026-09-19 (Fable 5.1)。ユーザー要望: 「重賞のデータは参加することが多いので、特別に過去の傾向やデータを一覧で見れる特別なページにしたい」。

## 0. 調査済みの事実 (前提・変更不可)

- 過去データは `ability.db` (SQLite, 500MB, gitignore, **封印 SHA-256 で管理・変更禁止**。読み取りは `file:...?mode=ro` で開く)。`runs` 26 列: date(YYYYMMDD), place, r, race_name, race_class, horse, sex(牡/牝/セ/''), age, jockey, kinryo, total_horses, umaban, popularity, rank, track_type(芝/ダート), distance, condition(良/稍/重/不), time_sec, chakusa, c4(4角位置), agari, pci, weight, affi('(美)'/'(栗)'/'美浦'/'栗東'/''), win_pay, fukusho_pay。index: (horse,date), (date), (place,track_type,distance,race_class,condition)。
- 重賞の識別:
  - **1986〜2025 (TARGET 由来)**: `race_class='重賞'`、`race_name` は **略称 + (Ｈ) + G1/G2/G3** (例 `菊花賞G1`, `アルゼンＨG2` = アルゼンチン共和国杯ハンデ, `みやこＳG3`, `エリザベG1`, `アメリカG2`)。略称は先頭 2〜5 文字 (ステークス→Ｓ, カップ→Ｃ 等の1文字略あり)。2015〜2025 で 156 キー、年 137〜140 レース、格の変遷 18 件 (例 大阪杯 G2→G1)。
  - **2026 (netkeiba 由来)**: `race_class='オープン'`、`race_name` は **正式名** `第75回日刊スポ賞中山金杯(GIII)` (回数・スポンサー冠・(GI/GII/GIII))。45 レース。
  - **当日出馬表 (jra_ev の STATE.races[].race_info)**: `【新潟 8R】芝2000m 15:45発走　農林水産省賞典 新潟記念` のように **冠 + 空白 + レース名、格の表記なし**。
  - 4 文字前方一致だけで 2026 名 → TARGET キーは 36/45 一致。不一致 9 件は全て冠付き (`日刊スポ賞中山金杯` `報知弥生ディープ記念` `フジTVスプリングS` `サンスポ杯阪神牝馬S` `東海テレビ杯金鯱賞` `中スポ賞ファルコンS` `デイリー杯クイーンC` `スポニチ賞京都金杯` `日刊スポシンザン記念`)。
- 配当: 全年 `win_pay`/`fukusho_pay` (行単位)。`race_payouts` (単勝/複勝/枠連/馬連/ワイド/馬単/三連複/三連単) は **2025〜2026 のみ**。
- 血統: `pedigree_cache.json` ({馬名: {sire, bms}}, 32,504 頭)。重賞出走馬のカバー率 2016: 9% / 2020: 80% / 2024: 100%。
- 既存ヘルパー (`api/past_data_service.py`): `calculate_waku(umaban, head_count)`、`_kyaku_from_c4(c4)` (1=逃げ, 2-4=先行, 5-9=差し, 10+=追込)。
- 統合版の構成: `jra_suite.py` の `_SECTIONS` (prefix/title/loop) にタブを追加し `create_app()` で Blueprint を `url_prefix` 付きで登録。テンプレは `jra_perf.py` (`bp = Blueprint`, `index_perf.html` を `send_from_directory`, `_no_cache_html`)。画面の見た目は `index_perf.html` の `.pf-*` スタイルに合わせる。

## 1. 設計

1. **表示用キャッシュ DB** `data/graded_cache.sqlite` を `build_graded_cache.py` で ability.db から生成する (ability.db は読み取りのみ・無変更)。ページはキャッシュだけを読む (即応・ability.db 500MB を毎回走査しない)。gitignore に追加。
2. **レース同定**: 正規化関数で TARGET 略称・netkeiba 正式名・当日出馬表名を同じ `race_key` に束ねる。
3. **新タブ「重賞データ」** (`/graded/`, Blueprint `jra_graded.py`, `index_graded.html`): 今週 (解析済み出馬表) の重賞をチップで並べ、任意の重賞も一覧から選べる。選ぶと過去 N 年 (既定 10) の結果表と傾向集計を一画面に表示する。
4. 予測・スコア・購入判断には一切関与しない (表示専用)。

## 2. 仕様

### 2.1 正規化 (`api/graded_names.py`、純関数・テスト対象)

```python
normalize_race_name(name) -> (base, grade | None)
race_key(base) -> str            # 4文字キー
match_key(name, known_keys, *, place=None, track_type=None, distance=None) -> (key | None, grade | None, how)
```
1. `normalize_race_name`: NFKC 正規化 → 先頭の `第N回` 除去 → `(GI|GII|GIII)` / 末尾 `G[123]` を格として抽出して除去 → 末尾 `Ｈ`/`H` (ハンデ印、TARGET 由来) を除去 → 全角/半角・空白を正規化。
2. `race_key`: `ステークス→Ｓ`, `カップ→Ｃ`, `トロフィー→Ｔ` に置換したうえで **先頭 4 文字** (4 文字未満はそのまま)。TARGET 略称はこのキーと (格を外して) 一致する。
3. `match_key`: 候補を順に試し、最初に既知キーへ一致したものを返す:
   a. base 全体のキー。
   b. **冠除去**: base 内の空白で区切られた最後のトークン (`農林水産省賞典 新潟記念` → `新潟記念`)。
   c. 冠パターン除去: 先頭から `(賞|杯|賞典)` で終わる最短の冠を 1 回だけ除去 (`日刊スポ賞中山金杯` → `中山金杯`, `サンスポ杯阪神牝馬S` → `阪神牝馬S`, `東海テレビ杯金鯱賞` → `金鯱賞`)。ただし除去後が 2 文字未満なら不採用。
   d. 冠リスト除去 (`data/graded_sponsors.json` に配列で保持、初期値: 日刊スポ賞, 日刊スポ, スポニチ賞, 報知, 報知杯, 東海テレビ杯, フジTV, フジテレビ賞, 中スポ賞, サンスポ杯, デイリー杯, 農林水産省賞典, 産経賞, 読売, 毎日, 関西テレビ放送賞, テレビ愛知, ラジオNIKKEI賞 は**除外** (レース名そのもの), 京都新聞杯 も除外)。
   e. **別名表** `data/graded_aliases.json` ({表記: key}) を最優先で参照 (手動メンテ用)。
   - `place/track_type/distance` が与えられたら、同名候補が複数のときは直近開催の条件が一致するキーを優先。`how` に `'alias'|'exact'|'last_token'|'strip_sponsor'|'sponsor_list'` を返す。
4. 格の変遷は無視してキーで束ねる (`グレードは年ごとに保持`)。

### 2.2 キャッシュ生成 `build_graded_cache.py`
1. 入力: `ability.db` (ro), `pedigree_cache.json`。出力: `data/graded_cache.sqlite` (既存があれば作り直す。原子的に: 一時ファイルに作って rename)。
2. 対象レース: `race_class='重賞'` **または** `race_name` が `(GI|GII|GIII)` を含む行 (2026)。障害重賞 (J・GI 等、`track_type` が芝/ダート以外や名前に `障害`/`ジャンプ` を含む) は除外。
3. テーブル:
   - `master(key PK, display_name, grade_latest, place_latest, track_type_latest, distance_latest, month_latest, first_year, last_year, n_years)`。`display_name` は最新年の正式名 (2026 netkeiba があれば冠・回数を除いたもの、無ければ TARGET 略称のまま。**略称のままのものは `outputs/t79_master_report.json` に列挙**して人手で `graded_aliases.json`/`display_name` 補正できるようにする)。
   - `races(race_id PK = date||place||r, key, date, year, grade, race_name_raw, place, track_type, distance, condition, head_count, win_time_sec, winner_pop, winner_pay, pay_umaren, pay_sanrenpuku, pay_sanrentan, same_course_as_latest INTEGER)`。配当は `race_payouts` から (無ければ NULL)。`same_course_as_latest` = master の最新 place/track/distance と一致するか。
   - `runs(race_id, umaban, waku, horse, sex, age, jockey, kinryo, weight, affi_norm('美'|'栗'|''), popularity, rank, c4, kyaku, agari, time_sec, chakusa, win_pay, fukusho_pay, sire, prev_date, prev_race_name, prev_place, prev_distance, prev_rank, prev_popularity, prev_interval_days)`。`waku = calculate_waku(umaban, head_count)`、`kyaku = _kyaku_from_c4(c4)`、`sire = pedigree_cache[horse].sire` (無ければ NULL)、前走は `idx_horse_date` で `horse` の直前 `date` の行 (ability.db 全行から。無ければ NULL)。
   - `meta(key, value)`: built_at, ability_db_sha256_prefix (先頭 1MB の sha256 ではなく `PRAGMA`… **簡易に** ファイルサイズ+mtime), n_races, n_runs。
4. `--years-from 1986` 既定、`--report` で master 報告 JSON を出す。実行時間の目安と件数を完了報告に含める。
5. `retrain_all.bat` の末尾 (存在する場合) に `python build_graded_cache.py` を 1 行追加 (失敗しても他工程を止めない: `|| echo ...`)。

### 2.3 Blueprint `jra_graded.py` (prefix `/graded`)
- `GET /graded/` → `index_graded.html`。`_no_cache_html` は jra_perf と同じ。
- `GET /graded/api/master` → `{races: [{key, display_name, grade_latest, place_latest, track_type_latest, distance_latest, month_latest, n_years, last_year}], built_at}`。並びは「今日以降で最も近い開催月日順」(month_latest + master 最新開催日の日付から)。キャッシュが無ければ `{error: "graded_cache.sqlite がありません。build_graded_cache.py を実行してください"}` を 503 で返し、画面にそのまま表示。
- `GET /graded/api/this_week` → `jra_ev.STATE["races"]` の各 `race_info` から名前部分 (全角空白の後) を取り、`match_key(name, known_keys, place=venue, ...)` で重賞と判定できたものを `[{venue, race_num, race_info, start_time, key, display_name, grade_latest, how}]` で返す (判定できないものは含めない)。`STATE` が空なら `[]` と `day_label`。
- `GET /graded/api/history?key=…&years=10&same_course=0|1` →
  ```
  {
    master: {...},
    course_history: [{year, place, track_type, distance, grade}],   // 条件変更の履歴 (年ごと)
    results: [ {year, date, place, track_type, distance, condition, head_count, win_time, winner_pop,
                pay: {win, umaren, sanrenpuku, sanrentan},
                top3: [{rank, umaban, waku, horse, sex, age, kinryo, jockey, popularity, kyaku, agari, sire, prev: {race_name, rank, popularity}}],
                runners: [ ...全出走馬 (同じ列) ... ] } ],                // 年降順
    aggregates: {
      popularity: [{label:'1番人気', n, c1, c2, c3, out, win_rate, ren_rate, fuku_rate, roi_win, roi_fuku}, '2番人気','3番人気','4-6番人気','7-9番人気','10番人気以下'],
      waku: 1..8 枠,
      kyaku: 逃げ/先行/差し/追込,
      sex_age: 牡3/牡4/牡5/牡6+/牝3/牝4/牝5+/セ (該当なしは省略),
      affi: 美浦/栗東,
      sire: 上位 8 (n>=3),  jockey: 上位 8 (n>=3),
      prev_race: 前走レース名 (正規化 base) 上位 10 (n>=3) + 前走着順帯 (1着/2-3着/4-9着/10着以下),
      weight: 馬体重帯 (〜439/440-479/480-519/520〜),
      interval: 前走間隔 (中1週以下/中2-3週/中4-8週/中9週以上)
    },
    upset: { fav_win_rate, fav_fuku_rate, avg_winner_pop, avg_win_pay, avg_sanrentan (2025+のみ, n付き), max_win_pay: {year, horse, pay} },
    n_years_used, years_range, same_course_filter
  }
  ```
  - 集計母集団: `rank` が数値の行 (取消・中止は除外)。`c1/c2/c3/out` は着別度数。`win_rate=c1/n`, `ren_rate=(c1+c2)/n`, `fuku_rate=(c1+c2+c3)/n`。`roi_win = Σ(win_pay where rank=1) / (n*100)`, `roi_fuku = Σ(fukusho_pay where rank<=3) / (n*100)` (**win_pay/fukusho_pay の意味 (勝者行のみか全行か・単位) を ability.db で実測してから式を確定し、完了報告に書く**)。
  - `years` は直近 N 開催 (年数ではなく開催回数)。`same_course=1` なら `same_course_as_latest=1` の開催のみ。
  - 結果表の `runners` は年ごと折りたたみ用 (既定は top3 表示)。

### 2.4 画面 `index_graded.html` (+ 必要なら `graded.js` を同ファイル内 `<script>` で)
1. 上部: 「今週の重賞」チップ列 (`this_week`。無ければ「解析済みの出馬表に重賞はありません / 解析開始で更新」)。その右に重賞セレクタ (`<select>` + 文字絞り込み `<input>`) と「直近 5 / 10 / 20 / 全開催」切替、「同一コースのみ」チェック。
2. 見出しカード: 表示名・最新格・コース (place/track/distance)・集計対象 (N 開催, 年範囲)・**コース変更注記** (course_history で条件が変わった年を列挙)。
3. 「過去の結果」表: 年 | 馬場 | 頭数 | 1着 (馬番/馬名/人気/性齢/斤量/騎手/脚質/父) | 2着 | 3着 | 勝ち時計 | 単勝配当 | 3連単 (あれば)。行クリックで全着順を展開。
4. 「傾向」: 2 列グリッドで人気別 / 枠番別 / 脚質別 / 性齢別 / 所属別 / 前走別 / 父 / 騎手 / 馬体重帯 / 間隔。各表は n・1-2-3-着外・勝率・連対率・複勝率・単回収・複回収。複勝率の最大行を薄く強調。
5. 「荒れ度」: 1番人気の勝率・複勝率、平均勝ち馬人気、平均単勝配当、最高配当 (年/馬)。
6. スタイルは `index_perf.html` の `.pf-*` を流用 (`style.css` 共通変数)。幅 400px でも横スクロールは表内のみ。
7. `?key=…` で直接開けるようにする (今週チップのクリックはこのクエリで再描画。iframe 内でも動くよう相対 URL)。

### 2.5 `jra_suite.py`
- `_SECTIONS` に `{"prefix": "graded", "title": "重賞データ", "desc": "重賞の過去傾向 (ability.db 由来キャッシュ)", "loop": None}` を **perf と race の間**に追加。`create_app()` で `jra_graded.bp` を `/graded` に登録。`_LOOP_*` は変更なし。

## 3. テスト (`tests/test_t79_graded.py`)
1. `normalize_race_name` / `race_key` / `match_key`: 表の各ケースが期待どおり (`第75回日刊スポ賞中山金杯(GIII)`→key `中山金杯`/G3、`農林水産省賞典 新潟記念`→`新潟記念`、`みやこステークス`→`みやこＳ`、`アメリカジョッキーC`→`アメリカ`、`アルゼンＨG2`→`アルゼン`/G2、`サンスポ杯阪神牝馬S`→`阪神牝馬`、`エリザベス女王杯`→`エリザベ`、`ラジオNIKKEI賞`→冠除去されない、別名表優先)。§0 の不一致 9 件がすべて一致すること。
2. `build_graded_cache.py`: 小さな ability.db 風フィクスチャ (runs 数十行: 2 レース×3 年、前走行、race_payouts 1 年分) からキャッシュを作り、`master/races/runs` の件数・waku/kyaku/sire/prev の値・same_course フラグを検証。
3. API: `jra_suite.create_app()` の test client で `/graded/`、`/graded/api/master`、`/graded/api/history?key=…&years=2`、`/graded/api/this_week` (jra_ev.STATE を monkeypatch: 重賞 1 件 + 非重賞 1 件) を検証。集計の勝率/ROI をフィクスチャの手計算と一致させる。キャッシュ無し時は 503 とメッセージ。
4. 既存 `tests/test_jra_suite.py` (タブ数の検査があれば更新) が通る。

## 4. 完了報告に含めること
- 実 ability.db でのキャッシュ生成: 所要時間、`master` 件数、`races`/`runs` 件数、**`display_name` が略称のままのキー数と一覧 (outputs/t79_master_report.json)**、2026 netkeiba 名 45 件の一致数、直近 4 週の当日出馬表名 (jra_logging.db `races.race_name` や monitored_races) の一致結果。
- `win_pay`/`fukusho_pay` の実測意味と採用した ROI 式。
- **稼働中の統合サーバー (port 5005) は再起動しないこと**。タブの反映は上位モデル/ユーザーが再起動して確認する。

## 5. やらないこと (別タスク候補)
- 出走馬ごとの当該重賞での過去成績・リピーター判定 (T79b 候補)。
- 予測スコアへの反映、購入推奨。
- 2025 以前の馬連/3連単配当の埋め戻し (race_payouts は 2025〜)。
- 地方交流重賞 (Jpn)・障害重賞。
