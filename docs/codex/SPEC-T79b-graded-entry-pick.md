# SPEC-T79b: 重賞データページで、過去傾向に該当する今回の出走馬をピックアップする

起票: 2026-09-20 (Fable 5.1)。ユーザー要望: 「重賞データは、それぞれの過去の傾向から今回の出走馬で該当する馬をピックアップしてください」。T79 (SPEC-T79) の追補。

## 0. 前提 (調査済み)
- 出走馬の属性は出馬表解析 `api/index.py:analyze_race_url(url, mode)` の `horses[]` にある: `num, name, odds, sex_age ("牡3"等), kg (斤量), jock, affi ("美浦"/"栗東"), sire, w_num (枠番, 正しい割当), current_weight, interval_days, iv, kyakushitsu (矢印表記 "◀◁◁◁"…), hist[] (直近走: race_name, place, course, corners "3-3-2-1", rank, total, raw に日付), scratched, status`。
- 統合版ではオッズ監視が同じ URL を解析済みなら `jra_ev.get_cached_analysis(url)` → `{"result": <analyze_race_url の戻り>, ...}` が取れる (T73b)。EV 状態 `jra_ev.STATE["races"][*]` には `url`, `venue`, `race_num`, `race_info`, `horses[].{num, pop, odds, win_prob, scratched}` (人気は `pop`)。
- T79 の履歴 API `/graded/api/history?key=&years=&same_course=` は `aggregates` (popularity/waku/kyaku/sex_age/affi/prev_race/sire/jockey/weight/interval の各行 `{label, n, c1, c2, c3, out, win_rate, ren_rate, fuku_rate, roi_win, roi_fuku}`) と `results` を返す。集計は `jra_graded.py` 内の関数で行っているので、サーバー内から同じ関数を呼べる。
- 脚質ラベルの定義は `past_data_service._kyaku_from_c4` (1=逃げ, 2〜4=先行, 5〜9=差し, 10+=追込)。

## 1. 設計
- 新 API `GET /graded/api/pick?key=<sub-key>&url=<レースURL>&years=10&same_course=0|1`: 出走馬ごとに属性を過去傾向の区分に当てはめ、区分ごとの複勝率を全体複勝率と比べて **合致スコア** を出す。スコア上位を「傾向ピックアップ」として返す。
- 画面: 今週の重賞チップで選んだレース (URL あり) のとき、見出しカードの下に「今回の出走馬と過去傾向の照合」セクションを出す。スコア順の表と、各馬の好条件/不利条件チップ。上位 3 頭を強調。**購入推奨ではない旨を明記** (「傾向の一致度。予測スコア・購入判断には使っていません」)。
- 予測モデル・EV・仮想購入には一切関与しない。

## 2. 仕様

### 2.1 出走馬属性の導出 (`api/graded_pick.py`、純関数中心・テスト対象)
`derive_entry_attrs(horse, ev_horse=None, race_date=None) -> dict`:
| 区分 | 導出 | 集計ラベルへの対応 |
|---|---|---|
| popularity | `ev_horse.pop` (あれば) → 無ければ `horses` のオッズ昇順の順位 | 1/2/3番人気, 4-6, 7-9, 10番人気以下 (T79 と同じ境界) |
| waku | `w_num` | "1"〜"8" |
| kyaku | `hist[:3]` の各走 `corners` の **最後の数字** (4角) を `_kyaku_from_c4` で判定し最頻値 (同数なら直近優先)。corners 無しなら `kyakushitsu` 矢印: `◀◁◁◁`→逃げ, `◀◀◁◁`/`◁◀◁◁`→先行, `◁◀◀◁`/`◁◁◀◁`→差し, `◁◁◀◀`/`◁◁◁◀`→追込, `ー`→None | 逃げ/先行/差し/追込 |
| sex_age | `sex_age` を T79 の sex_age ラベル規則に変換 (牡3/牡4/牡5/牡6+/牝3/牝4/牝5+/セ) | 同左 |
| affi | `affi` | 美浦/栗東 |
| prev_race | `hist[0].race_name` を `graded_names.normalize_race_name` → `race_key` → **`match_key` で重賞キーに解決できればそのキー**、できなければ正規化名 (T79 のキャッシュ側 `prev_race_name` と同じ規則で作られていることを確認し、必要なら T79 側の正規化関数を共用する) | T79 の prev_race ラベル |
| prev_rank | `hist[0].rank` | 1着 / 2-3着 / 4-9着 / 10着以下 |
| sire | `sire` | 父ラベル |
| jockey | `jock` | 騎手ラベル (表記ゆれ: 全角/半角・スペース除去で比較) |
| weight | `current_weight` | 〜439/440-479/480-519/520〜 |
| interval | `interval_days` | 中1週以下(≤14日)/中2-3週/中4-8週/中9週以上 (T79 と同じ境界) |
取消 (`scratched`) の馬は対象外。

### 2.2 合致スコア (`score_entry(attrs, aggregates, overall_fuku_rate) -> {score, hits, misses, detail}`)
- 各区分 f について、馬の区分ラベルに一致する集計行 `row` を探す (無ければスキップ)。
- 収縮つき対数比: `lift_f = log((row.fuku_rate * n + overall * k) / ((n + k)) / overall)`、`k = 10` (n が小さいほど全体率に引き戻す)。`n = row.n`。`overall` = その履歴の全出走馬複勝率 (= Σ3着内 / Σ出走)。
- `score = Σ_f lift_f`。区分の重複を避けるため対象は `popularity, waku, kyaku, sex_age, affi, prev_race, prev_rank, sire, jockey, weight, interval` の 11 区分 (人気は「市場」を含むので **人気を除いたスコア `score_ex_pop` も返す**。画面の既定表示は `score_ex_pop`、切替で人気込み)。
- 表示用判定: `lift_f ≥ log(1.25)` かつ `n ≥ 8` → `hits` に `{factor, label, n, fuku_rate, c123: "c1-c2-c3-out"}`; `lift_f ≤ log(0.7)` かつ `n ≥ 8` → `misses`。`detail` に全区分の `{factor, label, n, fuku_rate, lift}`。
- `sire`/`jockey`/`prev_race` は集計に上位 8〜10 しか無いので、該当行が無ければ「データ少」としてスキップ (0 扱い)。

### 2.3 API `GET /graded/api/pick`
1. `url` が無い/不正 (JRA `accessD.html?CNAME=` で始まらない) → 400。
2. 解析結果: `jra_ev.get_cached_analysis(url)` があればその `result`。無ければ `index.analyze_race_url(url, "簡易")` を呼ぶ (数秒かかる。例外時は 502 と `{error}`)。EV 状態の同 URL レースがあれば `horses[].pop` を人気に使う。
3. 履歴集計: T79 の内部関数で `key/years/same_course` の `aggregates` と `overall_fuku_rate` を得る (HTTP 経由で自分を呼ばない)。
4. 返却: `{race: {venue, race_num, race_info, url, source: "cache"|"scrape"}, key, display_name, n_years_used, overall_fuku_rate, entries: [{num, name, pop, odds, attrs, score, score_ex_pop, hits, misses, detail}], picks: [num…(score_ex_pop 上位3・同点は人気順)]}`。`entries` は `score_ex_pop` 降順。
5. `this_week` の各要素に `url` を追加する (既に無ければ)。

### 2.4 画面 `index_graded.html`
1. 今週チップ選択時に `state.url` を保持し、履歴描画後に `api/pick` を呼ぶ (セレクタから選んだだけの場合は URL が無いので出さない)。
2. セクション「今回の出走馬と過去傾向の照合」: 注記「傾向の一致度 (参考)。予測スコア・購入判断には使っていません」。表: 順位 | 馬番 | 馬名 | 人気 | 一致度 (score_ex_pop を 2 桁) | 好条件 (hits を `枠3 (複勝30%・n10)` 形式のチップ) | 不利条件 (misses)。上位 3 頭の行を強調 (`.gd-pick`)。
3. 「人気込みで並べ替え」トグル。「取得中…」「解析結果がありません (解析開始で取得)」の状態表示。
4. 行クリックで `detail` (全区分の n・複勝率・lift) を折りたたみ展開。

## 3. テスト (`tests/test_t79b_graded_pick.py`)
1. `derive_entry_attrs`: corners "3-3-2-1"×2 走 + "5-5-6-7" → 逃げ (最頻/直近)、矢印フォールバック各パターン、sex_age 変換 (牡6→牡6+, 牝5→牝5+, セ4→セ)、weight/interval 境界、prev_race が重賞キーに解決される例 (`神戸新聞杯` → `神戸新聞`) と非重賞例。
2. `score_entry`: 手計算の小さな aggregates で lift・hits/misses・n<8 の除外・人気除外スコアを検証。
3. API: `jra_suite.create_app()` test client、`jra_ev.get_cached_analysis` と `jra_ev.STATE` を monkeypatch、T79 のフィクスチャキャッシュを使って `entries` の並び順と `picks` を検証。URL 不正 → 400。
4. `index_graded.html` に文言「購入判断には使っていません」とセクション id が含まれる。既存 `tests/test_t79_graded.py` が通る。

## 4. 報告に含めること
- 実データ確認: 本日 (9/20) の出馬表がオッズ監視で解析済みなら、稼働中サーバーには触らず、`index.analyze_race_url` を直接呼んでセントライト記念 (中山11R) とローズS (阪神11R) の `entries` 上位 5 頭 (馬番・馬名・score_ex_pop・hits) を報告。解析できない場合はその旨。
- **禁止事項** (前回と同じ): ability.db 書き込み禁止、5005 へ接続・POST・再起動しない、`git checkout/stash/reset` 禁止、`docs/TASKS.md` は触らない、コミットしない。他作業者の未コミット変更に触らない。

## 5. やらないこと
- スコアの検証・的中率評価 (表示専用の参考指標。評価するなら別途 prospective で記録)。
- 予測モデルへの組み込み。
