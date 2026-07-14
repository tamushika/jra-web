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
- jockeyはTARGET短縮名、JRAフルネーム、空白、減量記号の差があるため、主キーや単独の
  MOVE/DELETE根拠に使わず、補助確認・レポート列に限定する。

### 5.3 coverageの制約

ability.db生成経路では、取消、除外、中止、失格、障害競走などが含まれない場合がある。
したがってtruthに行がないことは、Neon行が誤りである証明ではない。
truth 0件または複数候補は必ず `UNRESOLVED` とする。

### 5.4 payload比較契約

「強いpayload」や「一致」を実装者判断に委ねない。比較は次の表に従う。

| Neon `races` | ability `runs` | 正規化・比較 | 役割 |
|---|---|---|---|
| `date` | `date` | §5.2の6桁→8桁変換後に完全一致 | 必須 |
| `place` | `place` | 比較用文字正規化後に完全一致 | 必須 |
| `馬名` | `horse` | NFKC・空白正規化後に完全一致 | 必須 |
| `horse_number` | `umaban` | 整数として完全一致 | 必須 |
| `rank` | `rank` | 整数値として完全一致。99、非整数、範囲外は証拠不足 | 必須 |
| `time` | `time_sec` | 両方を0.1秒単位の整数へ変換して完全一致 | 必須 |
| `track_type` | `track_type` | `ダ`/`ダート`等を既存規則でcanonical化して完全一致 | 必須 |
| `distance` | `distance` | 整数として完全一致 | 必須 |
| `total_horses` | `total_horses` | 整数として完全一致 | 必須 |
| `agari_3f` | `agari` | 両方がある場合は0.1秒単位で完全一致 | 整合性veto |
| `corner_4` | `c4` | 両方がある場合は整数として完全一致 | 整合性veto |
| `weight` | `weight` | 両方がある場合は整数として完全一致 | 整合性veto |
| `condition` | `condition` | 既存の馬場状態canonical化後、両方がある場合は一致 | 整合性veto |
| `jockey` | `jockey` | raw/normalized値と一致可否をレポート | 補助のみ |
| `race_name` | `race_name` | raw/normalized値と一致可否をレポート | 補助のみ |

Neon `time` は `M:SS.d` または秒表現、ability `time_sec` は数値秒として受け取り、
丸め誤差を避けるため両方を整数decisecondへ変換する。入力精度は0.1秒とし、許容差は設けない。
書式不明、NULL、変換不能は必須証拠不足とする。

`KEEP_CURRENT`、`MOVE_CANDIDATE`、DELETE時の保持行は、必須列がすべて存在・一致することを要求する。
整合性veto列は、片方がNULLなら判定材料にしないが、両方が存在して不一致ならMOVE/DELETEを許可しない。
jockeyとrace_nameは表記差が大きいため、単独の許可条件にも拒否条件にもせず人手確認用とする。
必須証拠が1項目でも欠ける場合は `UNRESOLVED` とする。

## 6. `fix_races_2025_dups.py` の詳細仕様

### 6.1 対象抽出

次の共通predicateを満たし、自然キーが重複する2025年行を対象とする。

```sql
date IS NOT NULL
AND place IS NOT NULL
AND race_num IS NOT NULL
AND horse_number IS NOT NULL
```

重複行に加え、各MOVE候補の移動先自然キーに存在する全行、そこから参照される移動先を
「対象閉包」として取得する。34,219件の `place IS NULL` 行および履歴の `race_num IS NULL` 行は対象外。

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
- `UNRESOLVED`
  - truth 0件/複数件、coverage外、日付不正、比較列不正、payload不一致、移動先競合、
    保持行不明、または上記分類の証明条件を満たさないすべてのケース。

7月4日の調査で確認された、horse/timeは一方のレースと一致するが
rank/jockey/race_name等が別レース由来となる混成行は、馬名だけで `race_num` を更新しない。
完全な同一truth整合が証明できなければ `UNRESOLVED`、正しい完全行が別に存在して
冗長性を証明できる場合だけ `DELETE_REDUNDANT_ERROR` とする。

### 6.3 全体操作計画

個別分類後、全対象閉包をメモリ上で同時に適用した最終状態をシミュレーションする。

- 全MOVEのsource/targetと、targetに存在する現在行を列挙する。
- 同一targetへ複数MOVE、別馬によるtarget占有、対象外行との衝突を検出する。
- 連鎖・循環を明示検出する。実装が一括最終状態として安全に扱えない連鎖・循環は
  `UNRESOLVED` とする。
- DELETE後に保持すべきtruth行が1件残ることを検証する。
- シミュレーション後の自然キー集合が一意であり、全変更行がtruthと一致することを検証する。
- 予定UPDATE件数、DELETE件数、KEEP件数、最終総行数差を確定する。

1件でも `UNRESOLVED`、target衝突、最終重複があればplanは `NOT_APPLICABLE` とする。
dry-runレポートは出力するが、`--apply` はDB mutation前に終了する。

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

論理plan hashには変動する `ctid` を含めず、完全同一行はoccurrence countを含むmultisetとして扱う。
CSV/JSONL/manifestの書き込み、再読込、件数、hashの検証が完了しない場合はplan生成失敗とし、
apply可能な成果物として扱わない。

### 6.5 apply前提とtransaction

`--apply` は少なくとも承認済みplanとそのSHA-256の明示指定を要求する。
実行手順は以下とする。

1. `races` writerが停止済みであることを運用チェックする。
2. 新規接続・単一transactionを開始し、最初の対象queryより前に
   `LOCK TABLE public.races IN SHARE ROW EXCLUSIVE MODE` を取得する。lock timeoutまたは取得失敗は
   mutation前にrollbackする。このlockを任意扱いにせず、未存在targetへのphantom INSERTも防ぐ。
3. ability.dbを再びread-onlyで開き、SHA-256が承認済みmanifestと完全一致することを確認してから、
   対象閉包を再取得・ロックし、再分類・全体計画を行う。
4. 現在の論理行集合・plan hashが承認済みmanifestと一致することを検証する。
   差異があればデータドリフトとしてmutation前にrollbackする。
5. apply直前の全対象閉包を新しいバックアップbundleへ保存し、hash・件数を検証する。
6. 同一transaction内で取得した `ctid` と全弁別列を使う。
   弁別列は `IS NOT DISTINCT FROM` でguardし、各UPDATE/DELETEのaffected row countを1件、
   `RETURNING` の内容を予定行と一致させる。
7. 全操作後にcommit条件を検証し、すべて成功した場合だけcommitする。

dry-run時の `ctid` をapplyへ渡してはならない。guard 0件/複数件、truth hash変化、
バックアップ失敗、plan drift、DB例外のいずれも全rollbackとする。

### 6.6 commit条件

以下をすべて同一transaction内で満たすこと。

- 共通predicate上の自然キー重複が0件。
- `UNRESOLVED` が0件。
- 実UPDATE/DELETE件数とplan予定件数が一致。
- 総行数の差が予定DELETE件数と一致し、UPDATEで行数が変わっていない。
- 各KEEP/MOVE後の行が§5.4の必須列すべてで期待truthと一致し、整合性vetoに不一致がない。
- 各DELETEに対応する保持行が1件存在し、truthと一致。
- 対象閉包の最終自然キーが一意。
- ability.dbのSHA-256が承認済みmanifestおよびapply開始時と同一。

再実行時は対象重複0件のno-opとなり、追加変更を発生させない。

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

1. Codexが合成fixtureと非本番DBで実装・テストを完了する。
2. 上位モデルが本番NeonでREAD-ONLY dry-runを実行する。
3. 上位モデルが分類、`UNRESOLVED`、バックアップ、ability hash、plan hash、観測件数を確認する。
4. planが `APPLICABLE` の場合だけ、上位モデルが適用を明示承認する。
5. 旧updaterを含むすべての `races` writerを停止する。
6. `fix_races_2025_dups.py --apply` を実行し、commit条件を確認する。
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
- canonical plan hashが順序差で変化せず、内容差で変化する。

### 10.2 dry-run/apply安全性

- 既定実行ではDB mutationが0件。
- `UNRESOLVED` が1件でもあればapply前にmutation 0件で終了。
- 承認後の行変化、guard rowcount 0/複数、バックアップ失敗、hash不一致で全rollback。
- postcheckで重複が残る、truth不一致、予定件数不一致の場合に全rollback。
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
- updaterの `ON CONFLICT` と部分indexのpredicateが一致し、odds保全ロジックを維持する。
- SQLiteと専用非本番PostgreSQLのテストパスが成功する。
- updaterのエラー案内が実際のT34b適用手順を指す。
- Codexによる本番Neon変更は0件、ability.dbは無変更。

### 11.2 本番適用ゲート（上位モデル担当）

- READ-ONLY dry-runの全行分類とplanがレビュー済み。
- 観測基準338組との差異が説明され、差異があれば再承認済み。
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
