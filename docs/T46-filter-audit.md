# T46: 学習/ライブ/評価のフィルタ一貫性監査

作成: 2026-07-18 下位モデル (Sonnet)。起票: [SPEC-T46](codex/SPEC-T46-filter-consistency-audit.md)。
**READ-ONLY調査。コード変更なし。是正判断・重要度の最終評価は上位モデルが行う。**

## 0. 調査範囲・前提

| 経路 | 対象ファイル | 備考 |
|---|---|---|
| ①学習 | `backtest_ml.py` の `build_dataset()` | **`git show HEAD:backtest_ml.py` (HEAD版) を精読**。作業ツリー版は別タスク(未コミット)で編集中のため触れていない |
| ②ライブ | `api/index.py` (`analyze_race_url`, `parse_horse_row`, `get_race_class` 等)、`api/scoring.py`、`jra_ev.py` | 作業ツリー版(HEADと同一、差分なし) |
| ③評価 | `backtest_win5.py`、`backtest_ev.py`、`eval/blocks.py`・`eval/ledger.py`・`eval/cutoff.py`、`docs/evaluation-protocol.md` | `backtest_ev.py` は `backtest_ml.py` の `build_dataset`/`load_runs` を再利用 |
| (上流) | `build_ability_db.py` | ability.db (`runs`テーブル) を `1980.csv` から生成するスクリプト。学習・評価が共有する母集団フィルタの多くがここで発生している |

**除外したファイル** (指示による): `backtest_feature_pack.py` (他エージェントのWIPファイル、未読)、`outputs/` ディレクトリ、`backtest_ml.py` の作業ツリー版。

**データ検証**: `ability.db` (515MB, runsテーブル約184万行, 1986-01-05〜2026-07-04) に `sqlite3` の `file:...?mode=ro` 経由で読み取り専用接続して確認。`data/jra_logging.db` (実運用ログ、72レース分) も参考に確認。書き込みは一切行っていない。

---

## 1. マトリクス (フィルタ軸 × 経路)

凡例: 🟢=データソース段階で保証済み・スクリプト側の実装は不要、🔵=当該経路に固有実装あり、🟡=フィルタなし(意図的/許容)、🔴=経路間で規則が食い違う・欠落している

### 軸1: 地方交流レースの扱い

| 経路 | 規則 | file:line |
|---|---|---|
| 上流 (DB構築) | `PLACE_MAP`はJRA中央10場のみ (札幌/函館/福島/新潟/東京/中山/中京/京都/阪神/小倉)。マップ外の開催コードは`parse_place()`が`None`を返し、`if not date or not place or rank is None: continue`で行ごと破棄。地方交流レースは物理的にability.dbに入らない | `build_ability_db.py:30-33`(PLACE_MAP), `:37-42`(parse_place), `:142-143`(continue) |
| ①学習 | `build_dataset()`自体にvenue名での除外条件なし。ability.dbが既に中央10場のみという前提に依存(🟢) | `backtest_ml.py` build_dataset全体 (該当コードなし) |
| ②ライブ | **制御なし(🔴)**。`analyze_race_url`はURLで指定されたレースをそのまま解析。URL探索が`accessD.html`+`CNAME=`のJRAサイト内リンクに限定されるためNAR単独開催は対象外だが、JRA開催内の「交流」競走(地方馬混合戦)を検知・除外・警告するキーワード判定は`api/index.py`・`jra_ev.py`とも0件 | `jra_ev.py:101-122`(find_entry_url), `api/index.py:385-416`(build_matrix_data) |
| ③評価 | `load_runs`は`date`範囲のみでSELECTし、場フィルタは独自実装なし。ability.dbが既に中央10場のみという前提に依存(🟢、①と同じ構造) | `backtest_ability.py:45-56`(load_runs) |

### 軸2: 障害レース

| 経路 | 規則 | file:line |
|---|---|---|
| 上流 (DB構築) | `track = g(row,"芝・ダ")`が「芝」「ダート」以外(障害等)なら`continue`で除外。コメント明記 | `build_ability_db.py:144-148` (`# 障害等は除外`) |
| ①学習 | `build_dataset()`にtrack_typeでの除外条件なし。データ構造上そもそも障害行が存在しないため不要という設計(🟢) | 該当コードなし |
| ②ライブ | **検知・除外の仕組みなし(🔴)**。`race_type = "ダート" if "ダート" in page_text else "芝"` という二値判定のみで「障害」という値自体が存在しない。障害コースのページは「ダート」の文字列を含まないことが多いため、**障害レースは高確率で「芝」に誤分類されうる**(＝芝の平地モデルで障害戦を採点することになる) | `api/index.py:443`, `:1013`(同型判定の別関数) |
| ③評価 | 評価スクリプト側にtrack_type判定コードなし。ability.dbが既に障害除外済みという前提に依存(🟢、①と同じ構造) | 該当コードなし |

### 軸3: 出走状態(取消・除外・中止・失格)

| 経路 | 規則 | file:line |
|---|---|---|
| 上流 (DB構築) | `parse_rank()`が着順文字列に「取/除/中/失」を含む場合`None`を返し、`rank is None`の行は破棄。取消等の行は物理的にability.dbに入らない | `build_ability_db.py:68-75`(parse_rank), `:142-143` |
| ①学習 | ability.db側で既に除去済みだが、`build_dataset()`内で`cur["rank"] is None`を再チェックする多重防御コードあり | `backtest_ml.py:256`(`if not prior`直前の`date_from`/`rank is None`チェック、旧行番号253-254相当) |
| ②ライブ **(既知課題T47、HEAD確認済み)** | `parse_horse_row()`の戻り値dictに`status`/`scratched`等のキーが一切無い(num, odds, iv, name, sex_age, kyakushitsu, kg, jock, affi, sire, dam, bms, current_weight, weight_change, histのみ)。一方`jra_ev.py`側は`h.get("status")`/`h.get("scratched")`等を読み出そうとするが、入力元が常に空のため機能しない。`field_size = len(runners)`は取消馬も含めてカウントされうる。**T40で追加された`odds_snapshots`テーブルの`stage`列等はこの問題と別軸(取得タイミング品質)であり、`status`/`scratched`列自体はテーブルスキーマにも存在しない**(`data/jra_logging.db`のpredictions/odds_snapshotsスキーマで確認) | `api/index.py:293-350`(parse_horse_row), `jra_ev.py:177-184`(_snapshot_runner), `jra_ev.py:196`(field_size), `docs/TASKS.md` T47 |
| ③評価 | ability.db側で既に除去済み。`backtest_win5.py`にも多重防御チェックあり | `backtest_win5.py:149`(score_all_runners), `:217`(popularity_races), `backtest_ml.py:256`相当(backtest_ev.pyが再利用) |

**備考**: T47は本監査より前(2026-07-18付TASKS.md)に既に上位モデルへ課題登録済みの既知事項。本監査はHEAD時点でも未修正であることを再確認したのみ(新規発見ではない)。

### 軸4: 履歴なし馬(n_prior=0)

| 経路 | 規則 | file:line |
|---|---|---|
| ①学習・backtest_ev.py (build_dataset使用) | **明示的に除外**。`prior = lst[max(0,i-4):i][::-1]; if not prior: continue` — 直近走ゼロ(デビュー戦)の行は学習データ(X,y)に一切追加されない | `backtest_ml.py:255-256`(HEAD版) |
| ②ライブ | 除外ではなく**フォールバック処理**。`api/scoring.py`の`_ml_features`は履歴が空でもデフォルト値(prev_rank=10, ln_interval=log(30)等)で全特徴を埋めるため、モデルロード済みなら数値スコアが出る。`compute_score()`は根拠が1つも無い場合のみ`(None, [])`。`jra_ev.py:compute_picks()`は`ml_score is not None`の馬のみをEV計算・通知対象に**暗黙**フィルタ(明示的な除外ロジックではない) | `api/scoring.py:846`(n_prior), `:1528-1538`(compute_score None条件), `api/index.py:894-928`(compute_score_ml), `jra_ev.py:243`(compute_picks) |
| ③評価・backtest_win5.py既定モード(score_all_runners) | **除外しない**。build_datasetのような`if not prior: continue`は無く、直近走が無い馬もレースに残る(能力成分`tbest/cbest/amin`はNoneのままバイアス成分のみでスコア計算・評価対象に含まれる) | `backtest_win5.py:105-209`(score_all_runners、該当除外コードなし) |

**🔴 3経路で規則が異なる**: 学習(build_dataset経由の評価=backtest_ev.py, backtest_win5.py --ml相当)はデビュー馬を完全除外して学習・評価するが、ライブは除外せずフォールバック値でスコアを出し(EV通知対象からは暗黙除外)、評価の既定モード(backtest_win5.py無印)はデビュー馬も評価に含める。**同一「デビュー馬」が経路によって「学習されない」「フォールバック値で表示される」「評価に混ざる」の3通りの扱いを受けている。**

### 軸5: 頭数条件(WIN5「8頭以上」)

| 経路 | 規則 | file:line |
|---|---|---|
| ①学習 | フィルタなし。`total_horses`は`calculate_waku()`呼び出し・`r_ctx`辞書の特徴量計算入力として使われるのみ | `backtest_ml.py`内 該当除外コードなし |
| ②ライブ | フィルタなし。`total_horses`/`field_size`はレース情報表示とPhase C品質フラグ(`insufficient_odds`判定)にのみ使用、レース除外用途ではない | `api/index.py:535-537`, `jra_ev.py:196-197` |
| ③評価 | **評価集計関数のみに存在**。`measure_coverage(races, upset_map, kmax=8, min_r=9)`・`simulate_win5(..., min_r=9)`の両方で`if r is None or r < min_r or len(members) < 8: continue`。スコア計算自体を行う`score_all_runners`・`popularity_races`・`build_dataset`にはこのフィルタは存在しない | `backtest_win5.py:237-242`(measure_coverage), `:270-275`(simulate_win5) |

**🟢 一貫項目 (SPEC§2-5が懸念した「評価専用であることの確認」= PASS)**: 8頭以上フィルタはWIN5カバレッジ集計にのみ実装されており、学習・ライブ・評価のスコア計算自体には漏れていない。

### 軸6: オッズ条件(「オッズあり」判定)

**🔴 スクリプトごとに異なる定義が並存(SPEC言及のT40判明事項を再確認)**:

| 箇所 | 条件式 | 用途 |
|---|---|---|
| `jra_ev.py:147-152` `_valid_win_odds()` | `1.0 < odds < 999.0` かつ有限値 | Phase C品質(`valid_odds_count`) |
| `jra_ev.py:265-266` `compute_picks()` | `odds and odds > 1.0`(上限チェックなし) | EV計算のゲート。コード内コメント「既存の監視/通知経路が参照するodds_okは、T40以前の判定を維持する」と明記の**意図的な別系統** |
| `jra_ev.py:330-332` `odds_ok` | `(_to_float(...) or 0) > 1.0` を頭数の半数以上で満たすか | 監視ループの「全体オッズ取得済み」判定 |
| `api/scoring.py:445` | `odds <= 1.0 or odds >= 999` なら無効(=有効は`1.0 < odds < 999`) | 市場点(odds_k)計算のガード |
| `api/scoring.py:803` | `1.0 < odds < 999` | `ln_odds`特徴量計算 |
| `api/prediction_logging.py:85` | `win_odds <= 1.0` なら無効 | ログ保存時の妥当性チェック(上限なし) |
| `backtest_ml.py:350`(HEAD, build_dataset内) | `odds and odds > 1.0` を満たさなければ**除外せずln(30)を代入**(上限999チェックなし) | 学習特徴量`ln_odds`。**無効オッズは行除外ではなく補完** |
| `backtest_win5.py:205`(score_all_runners) | `odds and odds > 1.0`(上限なし) | 表示用オッズ加点 |
| `backtest_ev.py:80-81` | `not odds or odds <= 1.0: continue` で**候補から除外**(上限なし) | EV候補生成 |

整理すると、実質2系統に大別できる:
- **系統A (上限999あり)**: `jra_ev._valid_win_odds`, `api/scoring.py:445,803` — ライブ側のみ
- **系統B (上限なし・`odds>1.0`のみ)**: `jra_ev.compute_picks`, `backtest_ml.py`, `backtest_win5.py`, `backtest_ev.py`, `api/prediction_logging.py` — 学習・評価側全部＋ライブの一部(通知ゲート)

**さらに9軸目として発見: オッズの情報源そのものが経路間で異なる**。学習・評価はability.dbの`win_pay`(確定払或金から逆算した確定オッズ、レース結果が出た後の値)を使うのに対し、ライブは発走前スナップショット(発売状況により欠測・変動・999.0センチネルあり得る)を使う。数値の意味が「発走後に確定した単勝オッズ」と「発走前のある時点のスナップショットオッズ」で異なり、閾値定義の差以前に**そもそも比較可能な同一の量ではない**点は構造的差異として記録に値する。

### 軸7: 期間・年のハードコード定数

| 箇所 | 定数 | 値 |
|---|---|---|
| `backtest_ml.py:43-45`(学習) | `TRAIN_FROM,TRAIN_TO` / `TEST_FROM,TEST_TO` / `ADDITIONAL_TEST_FROM,ADDITIONAL_TEST_TO` | `20210101-20241231` / `20250101-20251231` / `20260101-20260630` |
| `backtest_win5.py:352-353`(評価, argparseデフォルト) | `--from` / `--to` | `20240101` / `20251231`(**学習のTRAIN_FROM=2021年とは別の既定値**) |
| `backtest_ev.py:39-40,46,59`(評価) | `PERIODS` / `load_runs`範囲 / train-test境界 | `("2025年","20250101-20251231")`, `("2026年1-6月(OOS)","20260101-20260630")` / `20210101-20260630` / 境界`20241231` |
| `docs/evaluation-protocol.md:3-6` | historical benchmark宣言 | 「2025年と2026年上期は既に複数回の判断に利用されているためhistorical benchmarkとする」という運用上の位置づけ |
| ②ライブ | ハードコードなし。`datetime.now().year`で当年を動的取得 | `api/index.py:362,435` |

**発見(9軸目寄り)**: 期間定数はスクリプトごとに個別にハードコードされており、単一の共有定数(例えば`TRAIN_FROM`を全評価スクリプトがimportする、等)にはなっていない。`backtest_win5.py`の既定`--from=20240101`は`backtest_ml.py`の`TRAIN_FROM=20210101`とも`TEST_FROM=20250101`とも一致しない独自値。値そのものが矛盾しているわけではない(用途が異なるため意図的な可能性が高い)が、**「期間定数の単一情報源が無い」こと自体が将来のドリフトリスク**として記録する。

### 軸8: レース番号(WIN5「9R以降」)

| 経路 | 規則 | file:line |
|---|---|---|
| ①学習 | フィルタなし。`build_dataset()`にレース番号(`cur["r"]`)での除外条件は存在しない | 該当コードなし |
| ②ライブ | `analyze_race_url`自体にレース番号フィルタなし(全レース番号を同一処理)。WIN5対象レースの絞り込みは別エンドポイント`_scrape_win5_target`にのみ存在し、監視ループ本体(`jra_ev.py`)には影響しない | `api/index.py:876-926`(_scrape_win5_target), `jra_ev.py:488-508`(本体スキャンは全レース対象) |
| ③評価 | `measure_coverage`/`simulate_win5`の`min_r=9`のみに存在(軸5と同一関数)。スコア計算自体(`score_all_runners`等)には伝播しない | `backtest_win5.py:237,242,270,275` |

**🟢 一貫項目**: 9R以降フィルタも軸5同様、評価集計にのみ実装されスコア計算・学習・ライブには漏れていない。

---

## 2. コード精読で追加発見した軸 (SPEC §2の8軸以外)

### 軸9: オッズ判定の「情報源」の違い (軸6参照、独立項目として記録)
学習・評価=ability.db確定オッズ(payout由来) vs ライブ=発走前スナップショット。数値の意味が異なる。詳細は軸6参照。

### 軸10: WIN5評価の曜日フィルタ
`simulate_win5()`は土日のみを対象とする: `if datetime.strptime(date,"%Y%m%d").weekday() not in (5,6): continue`。学習・ライブには対応する制約なし。WIN5が実際に土日開催のみである実務制約を反映した意図的フィルタと見られ、直ちに不整合とは言えないが、SPEC8軸に含まれていなかったため記録する。 (`backtest_win5.py:277-281`)

### 軸11: 評価の最小サンプル数閾値 (n<30)
`backtest_win5.py:260`で`if n < 30: continue`。これは母集団フィルタではなくカバレッジ表の**報告(表示)閾値**であり、算出そのものからは除外しない。混同しないよう記録する。

### 軸12: 同着(dead heat)レースの扱い(学習尤度)
`backtest_ml.py`のPlackett-Luce尤度構築で「同着は勝ち馬ごとに1イベントとして扱う。勝ち馬が特徴量構築から漏れたレース(勝ち馬が初出走等)は尤度に寄与しない」との扱いがある(軸4のデビュー馬除外と組み合わさり、勝ち馬がデビュー馬だったレースは学習尤度から静かに欠落する)。ライブ・評価側に対応する概念はない(そもそも尤度計算をしない)。 (`backtest_ml.py:691-692,711`付近)

### 軸13: T39/T40のスナップショット品質フィルタ (`eval/cutoff.py`) が評価バックテストに未接続
`eval/cutoff.py`は`DISQUALIFYING_QUALITY_FLAGS = frozenset({"insufficient_odds","catchup_burst","post_time_changed"})` + `is_stale=1`を除外する共通cutoff実装を持つが、**`backtest_win5.py`・`backtest_ev.py`はどちらも`odds_snapshots`テーブルを使わずability.dbの確定オッズを使う**ため、この品質フィルタ経路は現状この2スクリプトには接続されていない。T39/T44/T45等の新しいas-of評価基盤とbacktest_win5/backtest_ev(旧来のability.dbベース評価)は、**同じ「評価」という括りの中でもオッズ取得元・品質保証の枠組みが別系統**であることが分かった。 (`eval/cutoff.py:23-25`, `backtest_win5.py`/`backtest_ev.py`にodds_snapshots参照なし)

### 軸14: 発売前レースの扱い(ライブのみ存在する概念)
ライブはレースを除外するのではなく警告表示に留める: 「⚠️ 全レースでオッズが取得できていません(発売前の可能性)。EV判定にはオッズが必須のため該当馬は出ません」。レース自体はSTATEに残り、オッズ無効のためEV計算対象から実質外れる形。学習・評価には「発売前」という状態自体が存在しない(過去確定データのため)。 (`jra_ev.py:531-537`)

---

## 3. 実データ検証 (ability.db, read-only)

```
sqlite3接続: file:ability.db?mode=ro (書き込みなし)
```

| 検証項目 | 結果 |
|---|---|
| `place`の distinct 値 | 中京/中山/京都/函館/小倉/新潟/札幌/東京/福島/阪神 の**10件のみ**。地方(NAR)venueは0件 |
| `track_type`の distinct 値 | 「ダート」「芝」の**2件のみ**。「障害」は0件 |
| `rank`が NULL の行数 | **0件**(全期間184万行中) — 取消・除外・中止・失格は物理的に存在しない |
| `rank`の範囲 | 1〜24(19以上は大頭数レースの下位着順、取消マーカー等ではない) |
| runs 総行数 | 1,842,520行 (1986-01-05〜2026-07-04) |
| 2025年の distinct レース数 | 3,455レース |
| うち `total_horses>=8` | 3,355レース |
| うち `r>=9` | 1,151レース |
| うち `r>=9 AND total_horses>=8` (WIN5評価対象相当) | **1,137レース (全体の32.9%)** — 軸5「評価専用フィルタ」が実際に評価母集団を約1/3に絞り込んでいることを定量確認 |
| 2025年の「馬の初出走(career debut)行」件数 | **4,660行 / 70,947行 (約6.6%)** — 軸4の`if not prior: continue`除外規則が学習・backtest_ev.pyの評価対象から静かに落とす行数の目安(build_datasetの`prior`はdate_from以前の履歴も含むため厳密な一致ではなく近似値) |

`1980.csv`(build_ability_db.pyのソースCSV)自体は本監査では未確認(ability.dbに既に反映済みのため実データ検証としてはability.db側で十分と判断し、地方交流・障害の除外「前」の生の行数は確認していない)。

---

## 4. 不一致リスト (発見のみ。重要度は仮ラベル、是正判断は上位モデル)

| # | 軸 | 内容 | 仮重要度 |
|---|---|---|---|
| 1 | 軸2 障害 | ライブの`race_type`判定(`"ダート" if "ダート" in page_text else "芝"`)は障害レースを検知できず、高確率で「芝」に誤分類。学習・評価データには障害戦が一切含まれないため、誤って解析された場合はモデルが未知の母集団を採点することになる | 高(仮) |
| 2 | 軸1 地方交流 | ライブに「交流」競走を検知・除外・警告する仕組みが皆無。学習・評価データは地方交流を含まないため、母集団のミスマッチが起こりうる | 中〜高(仮) |
| 3 | 軸3 出走状態 | T47既知課題の再確認: `parse_horse_row`がstatus/scratched列を出力せず、`jra_ev.py`側の取消馬フィルタが機能しない。field_sizeに取消馬が算入されうる | 中(仮、T47で既に台帳登録済み) |
| 4 | 軸4 履歴なし馬 | デビュー馬の扱いが3経路で不統一(学習=完全除外/ライブ=フォールバック値で表示・EV通知のみ暗黙除外/評価既定モード=除外なし) | 中(仮) |
| 5 | 軸6 オッズ条件 | `odds>1.0`(上限なし)系統と`1.0<odds<999`(上限999)系統が並存。特にライブの`compute_picks`はコメントで意図的にレガシー系統を維持している旨明記されているが、学習・評価側もレガシー系統(上限なし)であるため、系統の分布は「ライブ内で2系統混在」「学習/評価は上限なし系統に統一」という構造 | 低〜中(仮、既知のT40関連論点) |
| 6 | 軸6/9 オッズ情報源 | 学習・評価=確定payout由来オッズ、ライブ=発走前スナップショット。同じ「オッズ」という名前だが生成過程が異なる量を比較している | 情報共有のみ(仮、設計上避けがたい面あり) |
| 7 | 軸7 期間定数 | 期間のハードコード定数がスクリプトごとに独立しており単一情報源がない(`backtest_win5.py`既定`--from=2024`が`backtest_ml.py`のいずれの定数とも一致しない) | 低(仮) |
| 8 | 軸13 | `eval/cutoff.py`の品質フィルタ基盤が`backtest_win5.py`/`backtest_ev.py`に未接続(別系統のability.db確定オッズを使用) | 情報共有のみ(仮、用途が違うため設計意図の可能性あり) |

## 5. 一貫していた項目 (負の結果も記録)

- **軸5 頭数条件(8頭以上)**: WIN5評価集計関数(`measure_coverage`/`simulate_win5`)にのみ実装され、学習(`build_dataset`)・ライブ(`api/index.py`/`api/scoring.py`)・評価のスコア計算自体(`score_all_runners`/`popularity_races`)には一切漏れていない。SPEC§2-5の懸念は**該当なし(健全)**と確認
- **軸8 レース番号(9R以降)**: 同上。評価集計にのみ存在し他経路に漏れなし。**健全**
- **軸1・軸2・軸3の「データソース段階での除外」**: 地方交流・障害・取消/除外/中止の3点は、学習と評価が**同一のability.db**を参照するため、この2経路間では完全に一致している(ability.dbが唯一の真実源であるため当然ではあるが、個別の除外条件を各スクリプトが独自実装しているわけではなく、DB構築スクリプト1箇所に集約されている点は保守性として良い設計)。不一致が生じているのはこの2経路とライブとの間のみ
- **T4市場ベースライン(`--ninki`)の母集団**: `backtest_win5.py`の`--ninki`モードは本スコアと**同一の`runs`(`load_runs`結果、同一date_from)**をそのまま使い、独自の絞り込みを行っていない。カバレッジ集計の8頭以上・9R以降フィルタも本スコアと人気順ベースラインの両方に同一関数(`measure_coverage`)・同一条件で適用されている。**「同一集団比較」の前提はT4ベースラインについては満たされている**ことを確認 (`backtest_win5.py:404-407,426,433-438`)
- **学習の期間分割ロジック自体**: `build_dataset`は日付フィルタを内部で持たず、`main()`側の`dates<=TRAIN_TO`等のブールマスクで事後分割する設計であり、学習/固定テスト/追加テストの3期間が二重計算や取りこぼしなく単一の`build_dataset`呼び出し結果から分割されていることを確認(境界日の重複・欠落なし)

---

## 6. 制約・未実施事項

- `backtest_feature_pack.py`は他タスクの未コミットWIPファイルのため一切読んでいない。低コスト特徴パック(T41)がフィルタ規則を追加/変更している可能性は本監査の範囲外
- `1980.csv`(ソースCSV)自体は未確認。地方交流・障害を「除外する前」の生データ行数は確認していない(ability.db時点での結果=0件のみ確認)
- `build_dataset()`を実際にPythonで実行してのUnit検証は行っていない(コード精読のみ)。軸4の「デビュー馬4,660行」はability.dbの career-first-appearance近似値であり、`build_dataset`内の`prior`ウィンドウ(date_from以前の履歴も考慮)とは厳密には一致しない概算値
- ライブ側の実データでの地方交流・障害レース解析結果は確認していない(`data/jra_logging.db`には72レース分の実績しかなく、いずれもJRA中央3場・芝ダートのみでこの種のケースは記録されていない。コード上の欠落を実データで再現・確認したものではない)
- Neon Postgres側(本番DB)の状態は未確認(spec上も対象外と判断)
