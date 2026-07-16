# SPEC T34b: races 部分ユニークインデックス + 2025年重複の安全な是正

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T34。
前提: [SPEC-T34](SPEC-T34-updater-dedup-fix.md) と
[T34調査記録](../T34-neon-dedup-investigation.md)。

本SPECは、上位モデルが本番Neonで実施した READ-ONLY preflight（2026-07-14）により、
当初の全体 `NOT NULL` / 全体 `UNIQUE` migration が実データに適用不能と判明したため、
履歴データを維持したまま今後の完全キー行を保護する設計へ是正するものである。

**Codexの担当は、実装、合成データおよび非本番環境での検証、運用手順の用意までとする。**
**採否、コミット、本番NeonでのREAD-ONLY dry-run、本番バックアップ、`--apply`、migration適用は
上位モデルが承認のうえ実施する。Codexは本番Neonへ接続・変更しない。**

## 1. 改定履歴と改定理由

### 2026-07-14 改定（T34b v2）

初版レビューで、基本方針は妥当だが、誤DELETE、誤UPDATE、無効インデックスの見逃しに
つながる曖昧さが確認された。以下の理由で仕様を改定する。

1. 初版には「truthで一意に確定できない場合はDELETE」と「判定不能は触らない」が併記されていた。
   判定不能を削除理由にすると正常行を失うため、判定不能は必ず `UNRESOLVED` とし、
   1組でも残れば変更前に `--apply` を中止する。
2. `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` は安全な再実行方法ではない。
   並行作成失敗後に同名の `INVALID` indexが残った場合や、同名の別定義indexがある場合にも
   `IF NOT EXISTS` が処理をスキップし得るため、catalogの定義・状態を明示検査する。
3. 初版のpredicateは `race_num` と `place` だけを検査していた。
   自然キーの一部がNULLの行を保護対象に含めないことを明確にするため、4列すべての非NULLを
   index、preflight、`ON CONFLICT` で同一predicateとして使用する。
4. dry-run時の `ctid` はUPDATEやVACUUMで変化し得る。
   後日のapplyへ持ち越さず、applyと同一transaction内で再取得した `ctid` のみを行識別補助に使う。
5. `race_num` の修正は行単位では安全でない。移動先の既存行、複数MOVEの同一宛先、連鎖・循環、
   混成payloadを含むため、全対象と移動先を閉包として事前計画し、最終状態が一意である場合だけ適用する。
6. 「Codexは本番未実行」と「Codexが実データ338組をdry-run」の実施主体が曖昧だった。
   Codexと上位モデルの責任範囲、およびSQLiteとPostgreSQLの検証範囲を分離する。
7. Neonとability.dbで日付形式が異なり、ability.dbに存在しない成績もある。
   日付変換、truthのcoverage、照合に使える列、補助列を明文化する。

### 2026-07-14 実装時安全補足（T34b v2.1）

独立実装レビューで、canonical plan SHA-256だけでは `manifest.json` 内のDB識別情報、
ability.db SHA-256、artifact SHA-256の承認後改変を検出できないことが判明した。planの内容が
不変でも別DB・別truth・差し替えbackupを参照できる承認境界の欠陥になるため、次を追加する。

1. `--apply` はcanonical plan SHA-256に加え、承認対象となった `manifest.json` 実ファイルの
   raw bytes SHA-256も明示指定し、JSON解析より前に一致を検証する。
2. manifestはformat、mode、DB識別、ability情報、件数、artifact、snapshotの固定schemaで
   検証し、未知・欠落・型違いのmetadataを拒否する。
3. lossless JSONLではfloat、Decimal、date/datetime、bytesを自己記述型タグで保存し、
   NULL・空文字だけでなく値型も区別する。未知型は文字列化せず安全停止する。
4. dry-runが成果物を生成できてもplanが `NOT_APPLICABLE` ならCLIは非0で終了し、
   自動処理がmigrationへ連鎖しないようにする。

### 2026-07-14 本番dry-run所見と修正（T34b v2.2 — 2026-07-16 Codex実装済み）

上位モデルが本番Neonで初回のREAD-ONLY dry-runを実行 (無変更) した結果、
候補676行+移動先40行=**716行すべてが `UNRESOLVED`**、planは `NOT_APPLICABLE`。
原因を分解すると、**唯一の不一致列は `time` で709/709件がmismatch**、他14列
(date/place/horse/horse_number/rank/jockey/track_type/distance/total_horses/
condition/corner_4/weight/agari_3f/race_name) はすべてmatch。

真因は**データの不整合ではなく `time` 解析の取りこぼし**。Neon `races.time` は
`M:SS.d` でも秒表現でもなく、**区切り無しの圧縮桁列** (末尾1桁=0.1秒、その上2桁=秒、
残り=分) だった。例:
- `"3137"` = 3分13.7秒 = 193.7s = 1937 deciseconds (= ability `time_sec` 193.7)
- `"1134"` = 1:13.4 = 73.4s = 734 / `"1143"` = 743 / `"1095"` = 695 — すべてtruthと一致。

v2.1時点の汎用 `to_deciseconds` はこれを「3137秒」等と誤解釈するため必須列 `time` が全滅し、
本来 KEEP/MOVE に解決できる行まで `UNRESOLVED` に落ちる (fail-safeなので誤DELETEは無い)。

**実装内容 (Codex, 2026-07-16)**:
1. Neon `time` 専用パーサを追加し、圧縮桁列 (M SS d / MM SS d、末尾=0.1秒) に対応した。
   `M:SS.d`・明示的な秒表現も引き続き受理し、桁数不足/非数字/矛盾は証拠不足
   (`UNRESOLVED`) とする。ability `time_sec` と `agari_3f` は汎用秒パーサのまま変更しない。
2. §5.2/§5.4の「Neon `time` 形式」を圧縮桁列を含むsource-specific契約へ訂正した。
3. 実値 (`"3137"→1937`, `"1134"→734` 等)、境界、不正形式、payload分類、
   `agari_3f` 非回帰のテストを追加した。
4. 修正後、上位モデルが本番dry-runを再実行し、`UNRESOLVED` が期待どおり縮小することを確認する
   (残る `UNRESOLVED` は truth不在/複数候補/coverage外の真に判定不能な組に限られるはず)。

Codex検証は専用 `75 passed / 2 skipped`、全体 `267 passed / 2 skipped`。本番Neonへの接続・
dry-run・mutationは行っていない。残ゲートは上位モデルによるREAD-ONLY再dry-runである。
初回dry-runの保存済みbundleをDB再接続なしで形式集計したところ、716件中709件が4桁digit-onlyで
新パーサにより解析でき、残る非digit 7件は安全に拒否された。この確認は現在snapshotでの再分類を
代替しないため、本番適用判断には上位モデルの再dry-runを必須とする。

### 2026-07-16 再dry-run所見とスコープ一般化（T34b v2.3 — Codex実装依頼）

上位モデルが v2.2 で本番READ-ONLY再dry-runを実行した (bundle:
`outputs/t34b/20260716T101022.996693Z_dry_run`、manifest_sha256
`7b7abe8f…`、plan_sha256 `4a5d9cdd…`)。結果:

- `time` は709/709一致 (v2.2修正は成功)。分類は KEEP 295 / MOVE 414 / DELETE 0 /
  **UNRESOLVED 7**。planは `NOT_APPLICABLE`。
- 残る阻害は3種: ①UNRESOLVED 7行、②「MOVE chain/cycle is unsupported」、
  ③シミュレーション最終状態の自然キー衝突4件 (すべて①の帰結)。

上位モデルの追加調査で判明した事実 (詳細は [調査記録](../T34-neon-dedup-investigation.md)):

1. UNRESOLVED 7行はすべて**競走中止馬** (rank NULL・time `"----"`)。ability.dbは
   中止馬を収録しないため truth 0件は設計どおり (§5.3)。netkeiba馬ページ照合の結果、
   7頭中2頭 (ハーフェズ・パルピタシオン) は現在のキーが正しく、**5頭はrace_num誤り**
   (小倉250208 u3/u5/u7: R4→R5、中山250412 u9: R1→R3、福島251116 u9: R4→R5)。
   この5行+中山250412のモーニングマジック行 (R3 u9→R1 u9、truth・netkeiba両方で検証済み)
   は**上位モデルがユーザー承認のうえ手動是正する** (ツール適用の前提。Codexスコープ外)。
2. 連鎖80組の実体は**レース単位のrace_num入れ替え** (例: 中山250316 R1↔R3)。
   全行がpayload検証済みMOVEであり、一括最終状態としての適用は安全。
3. **重複キーはNeon誤登録の一部に過ぎない**。2025年の完全キー行44,634行を
   ability truth と全件照合すると、**キーは一意だが馬名がtruthと不一致の行が2,589行**、
   キーがtruthに存在せず結果を持つ行が180行、非出走マーカー行 (rank NULL・
   time `"----"`/NULL) が330行ある。重複起点の候補抽出ではこれらに到達できない
   (例: 中山250412 R3 u9 のモーニングマジック行)。
4. 2026年は**全一致・不一致0** (1〜4月はnetkeiba独立truthで1,003/1,003一致)。
   本是正の対象は2025年のみでよい。

**v2.3 改定内容**:

1. **候補抽出の一般化 (§6.1改定)**: 共通predicateを満たす2025年行のうち、
   (a) 自然キーが重複する行、に加えて
   (b) 自然キーはtruthに存在するが馬名 (§5.4正規化後) がtruthと不一致の行、
   (c) 自然キーがtruthに存在しない行、
   も候補とする。対象閉包 (MOVE先の既存行・そこから参照される行) の規則は不変。
   分類・payload契約 (§5.4)・全体計画 (§6.3)・成果物 (§6.4)・apply安全策 (§6.5-6.6) は
   同一のものを適用する。観測基準値: 総候補は2,589+676+180+330の重複を除いた集合
   (行数は実行時に確定し、manifestに記録する。差異はデータドリフトとして扱う)。
2. **連鎖・循環の一括最終状態適用 (§6.3改定)**: 全operationを「同時に適用した最終状態」
   として計画・検証する現行設計を維持したまま、**payload検証済みMOVEのみで構成される
   連鎖・循環は適用可能とする**。部分UNIQUEインデックス作成前の適用であり順序制約は
   存在しないが、実装は防御として同一transaction内で全UPDATEを実行し、commit条件
   (§6.6: 最終自然キー一意・全行truth一致・件数一致) で最終状態を証明する。
   「chain/cycle unsupported」によるplan拒否は廃止する。検証不能行を含む連鎖・循環は
   従来どおり適用不可。
3. **非mutation分類の追加 (§6.2改定)**: 変更を伴わない2分類を追加する。
   - `NON_RUNNER_KEEP`: truth候補0件かつ rank IS NULL かつ time がNULLまたは
     正規化後 `"----"` の行。中止・取消馬の正当な行とみなし、mutationしない。
   - `UNVERIFIED_KEEP`: 上記に該当せず、truth 0件/複数件等で証明不能な行。
     mutationしない。
   両分類は**シミュレーション最終状態でその自然キーに衝突が無い場合に限り
   planをブロックしない** (レポートには全件残す)。最終状態で衝突する場合は従来どおり
   `UNRESOLVED` としてplanを `NOT_APPLICABLE` にする。DELETE/MOVE/UPDATEの実行条件は
   一切緩和しない (mutationは§5.4のtruth完全一致行に限る)。
4. **テスト追加 (§10.1)**: 2行スワップ (A↔B)、3循環、レース単位の完全スワップ
   (重複を生まない相互入替が候補(b)経由で検出・適用可能になること)、
   `NON_RUNNER_KEEP`/`UNVERIFIED_KEEP` が衝突無しで非ブロック・衝突有りでブロック、
   一般化抽出が既存の重複起点候補を包含すること。
5. 受け入れ基準 (§11.1) に「候補(b)(c)の抽出がREAD-ONLY snapshotで完結し、
   truth側の全行ロードがメモリ上限を持つこと (2025年truthは47,497行で全載せ可)」を加える。

責任分担は従来どおり: Codexは実装と合成/非本番検証まで。本番dry-run・手動是正・apply・
migrationは上位モデルが承認のうえ実施する。

### 2026-07-16 本文統合（T34b v2.3.1 — Codexレビュー反映）

Codexの仕様レビューで、v2.3が改定履歴のみで本文 (§5.3、§6.1-6.6、§9-11) が
v2.2のままである点、および以下の曖昧さの指摘を受けた。本改定で全て本文へ統合した。

1. `UNVERIFIED_KEEP` の「truth 0件/複数件**等**」が曖昧 → §6.2の**決定表**と
   `reason_code` 列挙で対象を閉じた。payload不一致・不正値も非mutationの
   `UNVERIFIED_KEEP` に落ちるが、**最終状態で自然キー衝突する行は例外なく
   `UNRESOLVED` (ブロック)** であり、mutation条件は一切緩和していない。
2. 非mutation行は「全行truth一致」のcommit条件と両立しない → §6.6を分離:
   mutation行とKEEP_CURRENTはtruth一致を検証、非mutation行 (`NON_RUNNER_KEEP`/
   `UNVERIFIED_KEEP`) は**自然キー・論理fingerprint・出現数が承認planと不変**であることを
   検証する。
3. 是正後も候補(c)は残るため「重複0件ならno-op」は不成立 → plan statusに
   `NO_MUTATIONS_NEEDED` を追加 (§6.3)。mutation 0件かつUNRESOLVED 0件なら
   CLIはexit 0で成功終了し、applyはno-op。
4. plan/manifestを**format v3**へ改版 (§6.4): 全分類の件数、行ごとの候補理由(a)(b)(c)、
   `reason_code` を固定schemaへ追加。`--apply` はformat_version 3以外のbundleを拒否する
   (旧v2 bundleでの適用は不可)。
5. 連鎖・循環の適用前に、**部分UNIQUEインデックス (または自然キー4列を覆う
   一意制約) が存在しないことをcatalogで検査** (§6.5)。存在すればmutation前に中止する。
6. 手動是正6行を§9の**明示的な前提ゲート**として追加 (2026-07-16 実施・検証済み)。
7. truthロードのメモリ上限を具体化 (§6.1): 上限超過はfail-closed停止。

## 2. 実データの現実（2026-07-14 READ-ONLY preflight）

`races` は2001～2025年を含む全履歴テーブルであり、1,232,092行ある。

- `race_num IS NULL`: 1,178,074行（95.6%）。`race_num` は2025年以降のみ充填
  （2025年: 44,634行、2026年: 9,384行）。
- `place IS NULL`: 34,219行。
- `(date, place, race_num, horse_number)` がすべて非NULLの重複は、観測時点で
  338グループ・676行、すべて2025年だった。
- 7月4日に修正済みの5組と異なり、同じ `(place, race_num, horse_number)` に別々の2頭が入る
  `race_num` 誤付与が中心で、単純な「後着行を削除」では直せない。
- SPEC-T34で作成済みのupdaterは `ON CONFLICT (date, place, race_num, horse_number)` を使うが、
  対応する一意制約/indexが未作成のNeonではinsertが失敗する。

上記の338グループは**観測基準値**であり、実装へ埋め込む固定値ではない。本番dry-run時に件数、
行集合またはplan hashが異なる場合は、データドリフトとして上位モデルの再確認・再承認を必要とする。

## 3. 目標と安全原則

### 3.1 目標

1. 履歴の不完全キー行を変更せず、4列の自然キーがすべて非NULLの行だけを一意に保護する。
2. 2025年の既存重複を、凍結truthとの一致を証明できる場合だけ是正する。
3. updaterの再登録をupsertで1行へ収束させ、`horse_odds=COALESCE` の既存保全挙動を維持する。
4. 不明点、状態変化、衝突、バックアップ不備があれば、書き込み前またはcommit前に安全停止する。

### 3.2 絶対条件

- truthが0件、複数候補、不整合、coverage外の行は削除しない。
- `UNRESOLVED` が1グループでもあれば、`--apply` はmutation開始前に中止する。
- 部分成功commitは禁止する。全計画を1transactionで適用し、全postcheck成功時だけcommitする。
- dry-runで取得した `ctid` をapplyで再利用しない。
- ability.dbはread-onlyで開き、内容・mtime・hashを変更しない。
- 本番資格情報や接続URLをバックアップ／ログへ出力しない。
- cleanup完了から有効なindex作成まで、旧updaterを含む `races` writerを停止する。

## 4. 成果物

### 4.1 重複是正スクリプト

- `fix_races_2025_dups.py`
- 既定は `--dry-run`。`--apply` を明示した場合だけ変更候補となる。
- 分類、全体計画、バックアップ、plan承認確認、transaction、postcheckを一つの実行経路で扱う。

### 4.2 migration一式

旧 `migrations/20260714_t34_races_natural_key.sql` の全体 `NOT NULL` / 全体 `UNIQUE` は
実行可能な状態で残さない。次の3段階を用意する。

1. `migrations/20260714_t34_races_natural_key_preflight.sql`:
   catalog状態と対象重複を確認するREAD-ONLY preflight。
2. `migrations/20260714_t34_races_natural_key.sql`:
   transaction外・autocommitで部分indexだけを作るmigration。
3. `migrations/20260714_t34_races_natural_key_postcheck.sql`:
   catalogの定義・有効性と対象重複0件を検証するpostcheck。

既存ファイル名を安全なindex作成用に置換する場合も、preflight/postcheckを別artifactとして用意する。
updaterのエラー案内は、実際に採用したファイル名と3段階の手順を指すよう更新する。

### 4.3 updater修正

- `jra_db_updater.build_races_upsert_sql`
- PostgreSQL/SQLite双方の部分indexと一致する `ON CONFLICT` predicate。
- 4列のいずれかが未確定の入力をDB書き込み前に拒否する既存ガードの維持・テスト。

## 5. ability.db truth契約

### 5.1 調査時点の既知事項

調査時点のlocal frozen ability.dbをread-onlyで確認した結果は以下のとおり。
この値は監査用baselineであり、件数を分類ロジックへ埋め込まない。

- 2025年 `runs`: 47,497行、2025-01-05～2025-12-28。
- `(date, place, r, umaban)` の重複: 0。
- `(date, place, horse)` の複数候補: 0。
- `date/place/r/umaban/horse/rank` のNULL: 各0。
- 確認時SHA-256:
  `EA6B8E5B989D722658170D2499342F98D82F01C5B875C3570ABF26C63112F061`。

スクリプトは実行ごとにability.dbのパス、size、mtime、SHA-256をmanifestへ記録し、
SQLite URIのread-only modeで開く。apply終了時にもhash不変を検証する。

### 5.2 日付と比較値の正規化

- Neon `races.date`: `YYMMDD`。2025年対象は `250101`～`251231` の6桁だけを受理する。
- ability.db `runs.date`: `YYYYMMDD`。Neon値の先頭へ機械的に `20` を付け、
  実在日付か検証したうえで比較する。
- 6桁/8桁以外、不正日付、2025年外は `UNRESOLVED` とし、推測で補正しない。
- place、horse等の文字比較は既存プロジェクトの正規化規則を優先し、NFKCと前後空白除去を
  比較用に適用する。raw値とnormalized値を両方ログへ残し、DB値自体は書き換えない。
- `rank`、`umaban` は整数へ厳格変換し、有効範囲外や解析不能を `UNRESOLVED` とする。
- Neon `races.time` はsource-specificに解釈する。NFKC・trim後のdigit-only文字列は、正規形の
  4桁 `MSSd` または5桁 `MMSSd` だけを圧縮時刻として受理する。末尾1桁を0.1秒、直前2桁を
  秒、残りを分とし、秒部60以上、5桁の先頭0、総時間0、4/5桁以外は証拠不足とする。
  `:` または小数点を含む文字列、およびPython数値型は明示的な秒表現として扱う。
  digit-onlyの短い文字列をtruth値に合わせて秒または圧縮時刻へ推測してはならない。
- ability `time_sec` は数値秒として汎用秒パーサで0.1秒単位へ変換する。Neon専用の圧縮解釈を
  ability値や `agari_3f` / `agari` へ適用しない。
- jockeyはTARGET短縮名、JRAフルネーム、空白、減量記号の差があるため、主キーや単独の
  MOVE/DELETE根拠に使わず、補助確認・レポート列に限定する。

### 5.3 coverageの制約

ability.db生成経路では、取消、除外、中止、失格、障害競走などが含まれない場合がある。
したがってtruthに行がないことは、Neon行が誤りである証明ではない。
truth 0件または複数候補の行を**MOVE/DELETE/UPDATEの対象にしてはならない** (証明不能)。
その行の分類は§6.2の決定表に従い `NON_RUNNER_KEEP` / `UNVERIFIED_KEEP` /
`UNRESOLVED` のいずれかとする (最終状態で自然キー衝突する場合は必ず `UNRESOLVED`)。
実例 (2026-07-16 確認): 競走中止馬はNeonに rank NULL・time `"----"` で存在するが
truthには無い。

### 5.4 payload比較契約

「強いpayload」や「一致」を実装者判断に委ねない。比較は次の表に従う。

| Neon `races` | ability `runs` | 正規化・比較 | 役割 |
|---|---|---|---|
| `date` | `date` | §5.2の6桁→8桁変換後に完全一致 | 必須 |
| `place` | `place` | 比較用文字正規化後に完全一致 | 必須 |
| `馬名` | `horse` | NFKC・空白正規化後に完全一致 | 必須 |
| `horse_number` | `umaban` | 整数として完全一致 | 必須 |
| `rank` | `rank` | 整数値として完全一致。99、非整数、範囲外は証拠不足 | 必須 |
| `time` | `time_sec` | Neon側は§5.2の圧縮/明示秒形式、ability側は数値秒として0.1秒単位の整数へ変換し完全一致 | 必須 |
| `track_type` | `track_type` | `ダ`/`ダート`等を既存規則でcanonical化して完全一致 | 必須 |
| `distance` | `distance` | 整数として完全一致 | 必須 |
| `total_horses` | `total_horses` | 整数として完全一致 | 必須 |
| `agari_3f` | `agari` | 両方がある場合は0.1秒単位で完全一致 | 整合性veto |
| `corner_4` | `c4` | 両方がある場合は整数として完全一致 | 整合性veto |
| `weight` | `weight` | 両方がある場合は整数として完全一致 | 整合性veto |
| `condition` | `condition` | 既存の馬場状態canonical化後、両方がある場合は一致 | 整合性veto |
| `jockey` | `jockey` | raw/normalized値と一致可否をレポート | 補助のみ |
| `race_name` | `race_name` | raw/normalized値と一致可否をレポート | 補助のみ |

Neon `time` のdigit-only文字列は `MSSd` / `MMSSd` の圧縮形式として扱い、`M:SS.d`、
小数点付き秒文字列、数値型の秒表現も受理する。ability `time_sec` は数値秒として受け取り、
丸め誤差を避けるため両方を整数decisecondへ変換する。入力精度は0.1秒とし、許容差は設けない。
Neon圧縮形式の秒部60以上、非正規桁数、書式不明、NULL、変換不能は必須証拠不足とする。

`KEEP_CURRENT`、`MOVE_CANDIDATE`、DELETE時の保持行は、必須列がすべて存在・一致することを要求する。
整合性veto列は、片方がNULLなら判定材料にしないが、両方が存在して不一致ならMOVE/DELETEを許可しない。
jockeyとrace_nameは表記差が大きいため、単独の許可条件にも拒否条件にもせず人手確認用とする。
必須証拠が1項目でも欠ける場合は `UNRESOLVED` とする。

## 6. `fix_races_2025_dups.py` の詳細仕様

### 6.1 対象抽出 (v2.3.1で一般化)

次の共通predicateを満たす2025年行のうち、以下のいずれかに該当する行を候補とする。

```sql
date IS NOT NULL
AND place IS NOT NULL
AND race_num IS NOT NULL
AND horse_number IS NOT NULL
```

- **(a) 重複**: 自然キーが同一の行が2行以上ある。
- **(b) truth不一致**: 自然キーがtruthに存在するが、馬名 (§5.4正規化後) がtruthの馬名と
  一致しない。
- **(c) truth不在**: 自然キーがtruthに存在しない。

各行は候補理由 (a/b/c、複数可) を保持し、分類・plan・manifestに記録する。
候補に加え、各MOVE候補の移動先自然キーに存在する全行、そこから参照される移動先を
「対象閉包」として取得する。34,219件の `place IS NULL` 行および履歴の `race_num IS NULL` 行は対象外。

観測基準値 (2026-07-16 上位モデル監査、手動是正6行の後): 2025年完全キー行44,634行中、
馬名一致41,535 / (b)相当2,589 / (c)結果あり180 / (c)非出走マーカー330。
基準値は実装へ埋め込まず、実行時観測との大きな乖離はデータドリフトとして
上位モデルの再確認を要する。

**メモリ上限 (fail-closed)**: truthは2025年分を全ロードしてよい (観測47,497行)。
truthロードが**200,000行**、候補+閉包が**100,000行**を超えた場合は、部分処理せず
エラー終了する (snapshotの前提が壊れている兆候として扱う)。

### 6.2 行の分類

各行は必ず次のいずれかに分類し、根拠、truth候補、比較結果を出力する。

- `KEEP_CURRENT`
  - 同一 `date/place` のhorse候補がtruthでexactly one。
  - truthの `r == race_num`、`umaban == horse_number`。
  - §5.4の必須payloadがすべて一致し、整合性vetoに不一致がない。
- `MOVE_CANDIDATE`
  - 同一 `date/place` のhorse候補がtruthでexactly one。
  - truthの `r != race_num` だが `umaban == horse_number`。
  - §5.4の必須payloadがすべて一つのtruth行と一致し、整合性vetoに不一致がない。
  - 馬名だけの一致ではMOVEを許可しない。
- `DELETE_EXACT_DUPLICATE`
  - 同じtruth行と一致する完全同一payloadが複数あり、決定的な規則で保持行を1件選べる。
  - truth一致が証明できない同一行はこの分類にしない。
- `DELETE_REDUNDANT_ERROR`
  - 対象行に混成または誤payloadがある。
  - 同じ正しいtruthを表す完全なNeon行が別に存在し、保持行を一意に特定できる。
  - 削除対象自身のnormalized `date/place/馬名` がexactly oneのtruthへ対応し、保持行も
    その同一truthへ§5.4の必須列すべてで対応する。
  - 対象行が冗長な誤登録であることを比較列と保持行で証明できる場合に限る。
- `NON_RUNNER_KEEP` (v2.3.1新設・非mutation)
  - 同一 `date/place` のhorse候補がtruthで0件。
  - かつ `rank IS NULL`、かつ `time` がNULLまたは正規化後 `"----"` (非出走マーカー)。
  - 中止・取消馬の正当な行とみなし、mutationしない。
- `UNVERIFIED_KEEP` (v2.3.1新設・非mutation)
  - MOVE/DELETE/KEEPの証明条件を満たさず、かつ非出走マーカーも満たさない行。
  - `reason_code` を必須で1つ付与する:
    `TRUTH_ZERO` (truth 0件・非出走マーカーなし) / `TRUTH_MULTI` (truth複数候補) /
    `PAYLOAD_MISMATCH` (truth 1件だが必須列不一致または整合性veto不一致) /
    `UMABAN_MISMATCH` (truth 1件・`umaban != horse_number`) /
    `INVALID_VALUE` (日付・整数・time等の変換不能)。
  - mutationしない。この列挙以外の事由を `UNVERIFIED_KEEP` にしてはならない
    (未知の事由は `UNRESOLVED`)。
- `UNRESOLVED` (ブロック)
  - 非mutation行 (`NON_RUNNER_KEEP`/`UNVERIFIED_KEEP` 相当) のうち、§6.3の
    シミュレーション最終状態で**自然キーが他行と衝突する**行。
  - MOVEの移動先競合、保持行不明、連鎖・循環に検証不能行が混在する場合、
    その他上記いずれの分類条件にも該当しないすべてのケース。

**決定表 (各候補行に上から順に適用)**:

| # | truth候補数 | 条件 | 分類 |
|---|---|---|---|
| 1 | 1 | 必須列全一致・veto不一致なし・`r==race_num`・`umaban==horse_number` | `KEEP_CURRENT` |
| 2 | 1 | 必須列全一致・veto不一致なし・`r!=race_num`・`umaban==horse_number` | `MOVE_CANDIDATE` |
| 3 | 1 | 必須列全一致・veto不一致なし・`umaban!=horse_number` | `UNVERIFIED_KEEP` (UMABAN_MISMATCH) |
| 4 | 1 | 必須列不一致またはveto不一致 | `UNVERIFIED_KEEP` (PAYLOAD_MISMATCH) |
| 5 | 0 | rank NULLかつtime NULL/`"----"` | `NON_RUNNER_KEEP` |
| 6 | 0 | 上記以外 | `UNVERIFIED_KEEP` (TRUTH_ZERO) |
| 7 | ≥2 | — | `UNVERIFIED_KEEP` (TRUTH_MULTI) |
| 8 | — | 比較値の変換不能 | `UNVERIFIED_KEEP` (INVALID_VALUE) |
| 9 | — | #1-8の結果が非mutationで、最終状態で自然キー衝突 | `UNRESOLVED` に昇格 |

DELETE系2分類の条件はv2から不変 (完全重複の決定的保持、冗長誤行の証明)。
mutation (MOVE/DELETE) の実行条件は一切緩和しない。

7月4日の調査で確認された、horse/timeは一方のレースと一致するが
rank/jockey/race_name等が別レース由来となる混成行は、馬名だけで `race_num` を更新しない。
完全な同一truth整合が証明できなければ非mutation (衝突時 `UNRESOLVED`)、正しい完全行が
別に存在して冗長性を証明できる場合だけ `DELETE_REDUNDANT_ERROR` とする。

### 6.3 全体操作計画

個別分類後、全対象閉包をメモリ上で同時に適用した最終状態をシミュレーションする。

- 全MOVEのsource/targetと、targetに存在する現在行を列挙する。
- 同一targetへ複数MOVE、別馬によるtarget占有、対象外行との衝突を検出する。
- 連鎖・循環を明示検出する。**payload検証済みMOVEのみで構成される連鎖・循環は
  適用可能とする** (v2.3.1)。全operationは「同時に適用した最終状態」として計画・検証し、
  applyは同一transaction内で全UPDATEを実行する。部分UNIQUEインデックス作成前
  (§6.5で不在をcatalog検査) のため順序制約は存在しないが、最終状態の一意性は
  本シミュレーションとcommit条件 (§6.6) の両方で証明する。
  検証不能行 (非mutation分類) を含む連鎖・循環は適用不可 (`UNRESOLVED`)。
- DELETE後に保持すべきtruth行が1件残ることを検証する。
- シミュレーション後の自然キー集合が一意であり、全mutation行がtruthと一致することを
  検証する。非mutation行 (`NON_RUNNER_KEEP`/`UNVERIFIED_KEEP`) は現在キーのまま
  最終状態に置き、衝突が生じる行は `UNRESOLVED` へ昇格する (§6.2 #9)。
- 予定UPDATE件数、DELETE件数、KEEP件数、非mutation件数、最終総行数差を確定する。

**plan statusは3値とする (v2.3.1)**:

- `APPLICABLE`: `UNRESOLVED` 0件・target衝突なし・最終重複なし・mutationが1件以上。
- `NO_MUTATIONS_NEEDED`: `UNRESOLVED` 0件かつmutation 0件 (候補が非mutation分類のみ)。
  CLIはexit 0で成功終了し、`--apply` を指定してもmutationなしで正常終了する。
  是正完了後の再実行はこの状態になる (非出走馬等の候補(c)は残り続けるため、
  「候補0件」をno-opの条件にしてはならない)。
- `NOT_APPLICABLE`: 1件でも `UNRESOLVED`、target衝突、最終重複がある。
  dry-runレポートは出力するが、CLIは非0で終了し、`--apply` はDB mutation前に終了する。

### 6.4 dry-run成果物

候補抽出、対象閉包、分類、バックアップ、件数取得は、PostgreSQLの単一READ-ONLY
`REPEATABLE READ` transaction/snapshot内で実行する。複数接続や異なるsnapshotの結果を
一つのplanへ混在させない。transaction途中でsnapshotを維持できなければdry-runを失敗とする。

dry-runごとにtimestamp付きディレクトリへ次を出力する。

- `classification.csv`: 1行ごとの分類、根拠、raw/normalized値、truth比較。
- `candidate_rows.csv`: 対象重複行の全列。
- `destination_rows.csv`: MOVE先に既存する行の全列。
- losslessな `rows.jsonl`: NULLと空文字を区別できる全列バックアップ。
- `plan.json`: source、action、target、保持行、論理行fingerprint。
- `manifest.json`: DB識別情報（資格情報を除く）、snapshot時刻、件数、ability.db情報、
  各ファイルSHA-256、canonical plan SHA-256、applicable判定、§5.4の必須列ごとの
  NULL・変換不能・不一致件数。
- **format v3 (v2.3.1)**: `format_version` は3とする。固定schemaに次を追加する:
  全分類 (`KEEP_CURRENT`/`MOVE_CANDIDATE`/DELETE系2種/`NON_RUNNER_KEEP`/
  `UNVERIFIED_KEEP`/`UNRESOLVED`) の件数、`UNVERIFIED_KEEP` の `reason_code` 別件数、
  候補理由 (a)(b)(c) 別件数、plan status (3値)。classification.csvとplan.jsonの各行にも
  候補理由と `reason_code` を持たせる。`--apply` は `format_version != 3` のmanifestを
  JSON解析後直ちに拒否する (v2以前のbundleで適用してはならない)。
- CLI出力の `manifest_sha256`: 上記manifest実ファイルのraw bytes SHA-256。自己参照を避けるため
  manifest内へ埋め込まず、plan SHA-256と組にして承認記録へ保存する。

論理plan hashには変動する `ctid` を含めず、完全同一行はoccurrence countを含むmultisetとして扱う。
CSV/JSONL/manifestの書き込み、再読込、件数、hashの検証が完了しない場合はplan生成失敗とし、
apply可能な成果物として扱わない。

### 6.5 apply前提とtransaction

`--apply` は承認済みmanifestのパス、manifest実ファイルSHA-256、canonical plan SHA-256の
3点すべての明示指定を要求する。manifest SHA-256はJSON解析より前に検証する。
実行手順は以下とする。

1. `races` writerが停止済みであることを運用チェックする。
2. 新規接続・単一transactionを開始し、最初の対象queryより前に
   `LOCK TABLE public.races IN SHARE ROW EXCLUSIVE MODE` を取得する。lock timeoutまたは取得失敗は
   mutation前にrollbackする。このlockを任意扱いにせず、未存在targetへのphantom INSERTも防ぐ。
3. **catalogで `public.races` に部分UNIQUEインデックス `uq_races_natural_key`、および
   自然キー4列を覆うその他の一意インデックス/制約が存在しないことを検証する** (v2.3.1)。
   存在する場合 (INVALID含む) はmutation前にrollbackする — 連鎖・循環の同時UPDATEは
   一意制約下では途中状態が違反になり得るため、cleanupは必ずインデックス作成前に行う。
4. ability.dbを再びread-onlyで開き、SHA-256が承認済みmanifestと完全一致することを確認してから、
   対象閉包を再取得・ロックし、再分類・全体計画を行う。
5. 現在の論理行集合・plan hashが承認済みmanifestと一致することを検証する。
   非mutation行 (`NON_RUNNER_KEEP`/`UNVERIFIED_KEEP`) についても、fresh再分類の結果が
   承認planと**分類・自然キー・論理fingerprint・出現数のすべてで一致**することを
   個別に検証する。差異があればデータドリフトとしてmutation前にrollbackする。
6. apply直前の全対象閉包を新しいバックアップbundleへ保存し、hash・件数を検証する。
7. 同一transaction内で取得した `ctid` と全弁別列を使う。
   弁別列は `IS NOT DISTINCT FROM` でguardし、各UPDATE/DELETEのaffected row countを1件、
   `RETURNING` の内容を予定行と一致させる。
8. 全操作後にcommit条件を検証し、すべて成功した場合だけcommitする。

dry-run時の `ctid` をapplyへ渡してはならない。guard 0件/複数件、truth hash変化、
バックアップ失敗、plan drift、DB例外のいずれも全rollbackとする。

### 6.6 commit条件

以下をすべて同一transaction内で満たすこと。

- 共通predicate上の自然キー重複が0件。
- `UNRESOLVED` が0件。
- 実UPDATE/DELETE件数とplan予定件数が一致。
- 総行数の差が予定DELETE件数と一致し、UPDATEで行数が変わっていない。
- 各 `KEEP_CURRENT`/MOVE後の行が§5.4の必須列すべてで期待truthと一致し、
  整合性vetoに不一致がない。
- **非mutation行 (`NON_RUNNER_KEEP`/`UNVERIFIED_KEEP`) は、自然キー・論理fingerprint・
  出現数が承認planおよびapply開始時と不変であること** (truth一致は要求しない。
  これらの行にtruth一致を要求してはならない — 証明不能行を無変更のまま残すのが契約)。
- 各DELETEに対応する保持行が1件存在し、truthと一致。
- 対象閉包の最終自然キーが一意。
- ability.dbのSHA-256が承認済みmanifestおよびapply開始時と同一。

成功後の再実行は `NO_MUTATIONS_NEEDED` となり (非mutation候補は残り続ける)、
追加変更を発生させない。「候補0件」や「重複0件のみ」をno-op判定に使ってはならない。

## 7. 部分ユニークインデックスmigration

### 7.1 index定義

index、preflight、updaterの `ON CONFLICT` は次のpredicateを完全に一致させる。

```sql
CREATE UNIQUE INDEX CONCURRENTLY uq_races_natural_key
ON public.races (date, place, race_num, horse_number)
WHERE date IS NOT NULL
  AND place IS NOT NULL
  AND race_num IS NOT NULL
  AND horse_number IS NOT NULL;
```

- `IF NOT EXISTS` は使用しない。
- `CREATE INDEX CONCURRENTLY` はBEGIN/COMMIT、`DO` block、migration runnerの暗黙transactionに
  入れず、autocommit接続で単独実行する。
- SQL実行器は `ON_ERROR_STOP` 相当とし、エラー後に次工程へ進まない。
- schemaは `public.races` と明示する。

### 7.2 作成前preflight

作成前にREAD-ONLYで以下を検査する。

1. 共通predicateを満たす自然キー重複が0件。
2. `uq_races_natural_key` という同名indexの有無。
3. 同名indexがある場合、対象table、列順、unique属性、predicate、
   `indisvalid`、`indisready` をcatalogから取得する。

判定は以下とする。

- 同名indexなし: 作成へ進む。
- 同一定義かつ `indisunique/indisvalid/indisready` がすべてtrue: 作成済みno-opとしてpostcheckへ進む。
- 同一定義だがINVALID/NOT READY: 自動修復・自動削除せず中止する。
- 同名だが別定義: 中止する。

INVALID indexの `DROP INDEX CONCURRENTLY` または `REINDEX INDEX CONCURRENTLY` は、
状態確認後に上位モデルが別途承認して実行する。失敗時もwriterを再開しない。

### 7.3 作成後postcheck

作成後に次を検証する。

- `indisunique/indisvalid/indisready` がすべてtrue。
- `pg_get_indexdef` と `pg_get_expr(indpred, ...)` が期待するtable、列順、predicateと一致。
- 共通predicate上の自然キー重複が0件。
- 同じ完全キーをupsertするsmoke testが1行へ収束する。本番確認は明示transaction内で実行して
  `ROLLBACK` し、実行前後の永続行数・値が不変であることも確認する。

名前が存在することだけを成功条件にしてはならない。

## 8. updaterの `ON CONFLICT`

`jra_db_updater.build_races_upsert_sql` は次の形へ変更する。

```sql
ON CONFLICT (date, place, race_num, horse_number)
WHERE date IS NOT NULL
  AND place IS NOT NULL
  AND race_num IS NOT NULL
  AND horse_number IS NOT NULL
DO UPDATE SET ...
```

- index predicateと文字上・意味上同じ条件を使い、PostgreSQLに部分indexを推論させる。
- 既存の `horse_odds=COALESCE(...)` 保全ロジックを維持する。
- 4列のいずれかがNULLの入力は、部分indexが拒否するとは仮定せず、アプリ側で登録拒否する。
- 一意index未適用・INVALID・別定義によりupsertできない場合はfallback INSERTを行わず、
  実際のpreflight/migration/postcheck手順を案内して失敗終了する。

## 9. 本番適用順序

**前提ゲート (v2.3.1)**: ability truthで証明不能な誤キー行の手動是正が完了していること。
2026-07-16に上位モデルがユーザー承認のうえ6行 (競走中止馬5行のrace_num修正+
モーニングマジック行の入替) を実施済み — バックアップ
`backups/t34b_neon_dnf_racenum_fix_20260716.csv`、是正後dry-runで最終キー衝突0件・
UNRESOLVED 2件 (正位置の中止馬ハーフェズ/パルピタシオンのみ。v2.3.1では
`NON_RUNNER_KEEP` となる想定) を確認済み。以後のdry-runでこのゲートに反する
衝突が再出現した場合は適用を中止し上位モデルが再調査する。

1. Codexが合成fixtureと非本番DBで実装・テストを完了する。
2. 上位モデルが本番NeonでREAD-ONLY dry-runを実行する。
3. 上位モデルが分類、`UNRESOLVED`、バックアップ、ability hash、manifest hash、plan hash、観測件数を確認する。
4. planが `APPLICABLE` の場合だけ、上位モデルが適用を明示承認する。
5. 旧updaterを含むすべての `races` writerを停止する。
6. manifestパス、承認済みmanifest SHA-256、承認済みplan SHA-256を明示して
   `fix_races_2025_dups.py --apply` を実行し、commit条件を確認する。
7. migration preflightを実行する。
8. autocommitで `CREATE UNIQUE INDEX CONCURRENTLY` を実行する。
9. catalog postcheckと非破壊smoke testを実行する。
10. 部分index対応済みupdaterを配備し、writerを再開する。

cleanup commitからindex有効化までにwriterを再開しない。index作成失敗、INVALID、別定義、
postcheck失敗時はwriterを停止したまま、上位モデルが復旧方針を判断する。

## 10. テスト

### 10.1 純ロジック／合成fixture

- 2頭が同スロットに入り、一方をKEEP、一方を正しいraceへMOVEできる。
- truth 0件、複数件、rank/time不一致、混成payloadは `UNRESOLVED`。
- 正しい完全行が別にある場合だけ冗長誤行をDELETE候補にできる。
- 完全重複は決定的な保持行1件を残す。
- 馬名だけ一致する行をMOVEしない。
- 移動先既存行、同一targetへの複数MOVE、連鎖、循環、対象外行との衝突を検出する。
- 不正な6桁日付、2025年外、Neon/ability日付変換を検証する。
- Neon圧縮timeの実値 (`3137→1937`, `1134→734`, `1143→743`, `1095→695`) と
  秒部60以上・桁不足・過剰桁・非数字を検証する。
- 圧縮timeでKEEP/MOVEが成立し、不正timeは非mutation分類となり、ability `time_sec` と
  `agari_3f` の従来の秒解釈が変わらないことを検証する。
- canonical plan hashが順序差で変化せず、内容差で変化する。
- **v2.3.1追加**:
  - 2行スワップ (A↔B)・3循環が検証済みMOVEのみなら `APPLICABLE` になり、
    最終状態が一意である。
  - レース単位の完全スワップ (重複を生まない相互入替) が候補(b)経由で検出・適用できる。
  - `NON_RUNNER_KEEP`/`UNVERIFIED_KEEP` は衝突なしで非ブロック、衝突ありで
    `UNRESOLVED` に昇格しplanをブロックする。
  - 決定表#1-9の各分岐と `reason_code` 5種の付与を網羅する。
  - 検証不能行を含む連鎖・循環は適用不可。
  - mutation 0件・UNRESOLVED 0件で `NO_MUTATIONS_NEEDED`・exit 0。
  - 一般化抽出(a)(b)(c)が従来の重複起点候補を包含する。
  - truthロード・候補閉包が上限超過でfail-closed停止する。

### 10.2 dry-run/apply安全性

- 既定実行ではDB mutationが0件。
- `UNRESOLVED` が1件でもあればapply前にmutation 0件で終了。
- 承認後の行変化、guard rowcount 0/複数、バックアップ失敗、hash不一致で全rollback。
- postcheckで重複が残る、truth不一致、予定件数不一致の場合に全rollback。
- **v2.3.1追加**: `format_version != 3` のmanifestを `--apply` が拒否する。
  自然キー4列を覆う一意インデックス/制約が存在するとmutation前に中止する。
  非mutation行のfingerprint/キー/出現数がapply時に変化していると全rollback。
  成功後の再実行が `NO_MUTATIONS_NEEDED` のno-opになる。
- apply直前backupが全列・移動先行を含み、NULLと空文字を区別できる。
- ability.dbがread-onlyで、実行前後のhash/mtimeが不変。
- 成功後の再実行がno-op。

### 10.3 SQLite

- 一時DBの部分unique indexで同一完全キーの再upsertが1行へ収束する。
- 4列のいずれかがNULLの行はindex対象外になる。
- 生成したSQLite用upsert SQLが既存のodds保全挙動を維持する。

SQLite成功だけでPostgreSQL固有動作を検証済みとはしない。

### 10.4 非本番PostgreSQL

本番 `DATABASE_URL` ではなく専用 `TEST_DATABASE_URL` だけを使用し、接続先が本番と同一なら拒否する。

- transaction外で部分unique indexを作成できる。
- 生成upsert SQLを2回実行して1行へ収束する。
- NULLを含むキーがindex対象外となり、アプリ側ガードは登録を拒否する。
- catalogがunique/valid/readyかつ期待定義・predicateである。
- 同名の有効同一定義、INVALID、同名別定義の3状態を正しく判定する。
- 対象部分indexを論理的に推論できない非含意predicateの `ON CONFLICT` が、期待する
  `42P10` で失敗することを確認する。

## 11. 受け入れ基準

### 11.1 Codex実装の受け入れ

- 旧全体 `NOT NULL` / 全体 `UNIQUE` migrationが実行可能な状態で残っていない。
- preflight、autocommit index作成、postcheckの各artifactと手順が用意されている。
- `fix_races_2025_dups.py` がdry-run既定で、分類、対象閉包、全体plan、CSV/JSONL backup、
  manifest/hashを出力する。
- `--apply` が承認plan照合、fresh再分類、transaction内guard、全postcheckを備える。
- 判定不能やtruth不存在をDELETEしないテストがある。
- Neon圧縮timeがsource-specificに解析され、ability `time_sec` / `agari_3f` を誤って
  圧縮形式として扱わない回帰テストがある。
- updaterの `ON CONFLICT` と部分indexのpredicateが一致し、odds保全ロジックを維持する。
- SQLiteと専用非本番PostgreSQLのテストパスが成功する。
- updaterのエラー案内が実際のT34b適用手順を指す。
- Codexによる本番Neon変更は0件、ability.dbは無変更。
- **v2.3.1追加**:
  - 候補抽出が(a)(b)(c)の3経路を実装し、READ-ONLY snapshot内で完結する。
  - §6.2の決定表と `reason_code` 列挙が実装され、列挙外の事由が `UNVERIFIED_KEEP` に
    落ちない。
  - 検証済み連鎖・循環の一括最終状態適用と、一意インデックス不在のcatalog検査がある。
  - plan/manifestがformat v3で、`--apply` が旧formatを拒否する。
  - `NO_MUTATIONS_NEEDED` の3値statusとexitコード、メモリ上限のfail-closed停止がある。
  - 非mutation行のcommit前検証 (fingerprint/キー/出現数不変) がある。

### 11.2 本番適用ゲート（上位モデル担当）

- §9の前提ゲート (手動是正6行、2026-07-16実施済み) が有効なままである
  (dry-runで最終キー衝突が再出現していない)。
- READ-ONLY dry-runの全行分類とplanがレビュー済み (`UNVERIFIED_KEEP` は
  `reason_code` 別件数を確認)。
- §6.1の観測基準値 (重複338組・(b)2,589行・(c)510行) との差異が説明され、
  差異があれば再承認済み。
- `UNRESOLVED == 0`、planが `APPLICABLE`。
- apply直前backupとmanifest/hashが検証済み。
- writer停止、cleanup、index作成、postcheck、updater再開の担当者と順序が確認済み。
- cleanup後の重複0件、indexのvalid/ready/unique、完全キーupsertの収束を確認済み。

## 12. やらないこと

- 全履歴への `race_num NOT NULL` / 全体UNIQUEの適用。
- 履歴117万行の `race_num` 一括backfill。
- `place IS NULL` 34,219行の是正。
- truth 0件、複数候補、不整合行の推測修正・削除。
- Codexによる本番Neonのdry-run、バックアップ、apply、migration適用。
- approvalなしのINVALID index削除・再構築。
- `jra_ev.py`、予測スコアリングなど登録アプリ外への変更。

## 13. 参考資料

- [PostgreSQL: CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html)
- [PostgreSQL: INSERT / ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html)
- [SQLite: Partial Indexes](https://www.sqlite.org/partialindex.html)
- [SQLite: UPSERT](https://www.sqlite.org/lang_upsert.html)
