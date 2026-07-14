# SPEC T34b: races 一意制約の設計是正 (部分ユニークインデックス) + 2025重複是正

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T34。
前提: [SPEC-T34](SPEC-T34-updater-dedup-fix.md) と [T34調査記録](../T34-neon-dedup-investigation.md)。
本SPECは、上位モデルが本番Neonで実施した **preflight (2026-07-14) の結果、
当初migrationが実データと不整合と判明したため設計を是正する**もの。

**Codexは実装と検証まで。採否・コミット・本番適用は上位モデル。**
`codex/T34b` ブランチか未コミットで作業。**本番Neonへの適用・破壊的是正は行わないこと**
(SQLとスクリプトの用意まで。実行は上位モデルが承認のうえ行う)。

## 実データの現実 (preflight 2026-07-14, READ-ONLY)
`races` は **1,232,092行の全履歴テーブル** (2001-2025含む)。当初想定の「recentな結果表」ではない。
- **race_num が NULL: 1,178,074行 (95.6%)**。race_numは2025以降のみ充填 (2025: 44,634 / 2026: 9,384)。
  → 当初migrationの `race_num SET NOT NULL` + 全体 `UNIQUE(date,place,race_num,horse_number)` は
  **構造的に適用不能** (履歴117万行のrace_numをNULLのままにできない)。
- place が NULL: 34,219行。
- **非NULLキーの重複: 338グループ (676行)・全て2025年**。7/4の5組 (是正済) 以外。症状が異なり、
  同一 (place, race_num, horse_number) スロットに**別々の2頭**が入る = **race_numの誤付与**
  (単純な誤行削除では直らない。正しいrace_numを判定して修正 or 誤登録行を削除する必要)。
- **連鎖問題**: SPEC-T34でコミット済のupdaterは `ON CONFLICT (date,place,race_num,horse_number)`
  を使うが、対応する一意制約/インデックスが未作成のため、**現状のNeonに対して新updaterで
  登録するとinsert自体が失敗する**。制約作成は新updater稼働の前提。

## 是正方針

### A. migration を「部分ユニークインデックス」に差し替え
`migrations/20260714_t34_races_natural_key.sql` を廃し、新migrationを作る:
- 全体NOT NULL/全体UNIQUEは**やめる**。代わりに **部分ユニークインデックス**:
  ```sql
  CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_races_natural_key
  ON races (date, place, race_num, horse_number)
  WHERE race_num IS NOT NULL AND place IS NOT NULL;
  ```
  → 履歴の race_num=NULL / place=NULL 行 (計~117万) には触れず、**今後のupdater登録行
  (race_num・place が必ず埋まる) だけを一意に保護**する。
- `CREATE INDEX CONCURRENTLY` はトランザクション外で実行する必要がある点に注意 (migrationを
  BEGIN/COMMITで包まない、または別ファイルに分ける)。
- **前提**: このインデックス作成は下記Bの2025重複是正が終わるまで失敗する (既存重複が
  述語を満たすため)。migration本文の冒頭に、対象述語での重複件数チェック→残っていれば
  中断 (RAISE) する preflight を入れ、**勝手に削除しない**。

### B. 2025年の338重複組を是正するスクリプト (実行は承認後)
`fix_races_2025_dups.py` (新規、`--dry-run` 既定) を作る:
- 対象: `date` の年が25、かつ (date,place,race_num,horse_number) が非NULLで重複する行。
- **裁定基準**: ability.db `runs` の2025年データ (1980.csv/TARGET由来・凍結truth) を正とする。
  各重複行の (馬名, rank, jockey) を、ability.db runs の同 (date,place,r,umaban) および
  「同 date・place で馬名が一致する別race」と照合し、
  - その行の (馬名, 成績) が **どの race_num に属するのが正しいか**を判定する。
  - スロット衝突は「片方のrace_numが誤り」のケースが主 → 可能なら**正しいrace_numへUPDATE**、
    truthで一意に確定できない/真の重複は**誤行をDELETE**。判定不能な組は**触らず報告**。
- **安全策 (7/4是正と同じ作法)**: `--dry-run` で分類結果を出力し全対象行をCSVバックアップ。
  実削除/更新は `--apply` 時のみ、ctid + 弁別フィールドをWHEREガードにトランザクションで実行、
  実行後に「対象述語での重複0」を検証してからcommit (不整合ならrollback)。
- 34,219件の place=NULL 行は部分インデックスの述語外なので**本タスクでは触らない** (別途)。

### C. updater の ON CONFLICT を部分インデックスに合わせる
`jra_db_updater.build_races_upsert_sql` を修正:
- `ON CONFLICT (date, place, race_num, horse_number) DO UPDATE ...` を、部分インデックスに
  推論が効くよう **`ON CONFLICT (date, place, race_num, horse_number)
  WHERE race_num IS NOT NULL AND place IS NOT NULL DO UPDATE ...`** にする。
- PostgreSQL と SQLite の両方で部分インデックス+ON CONFLICT述語が動くことをテストで確認
  (既存の horse_odds=COALESCE 保全ロジックは維持)。updater は race_num/place 未確定行を
  既に登録拒否するため、述語は常に成立する。

## テスト (最低3件)
1. 部分ユニークインデックス下で、同一 (date,place,race_num,horse_number) の再登録が
   upsertで1行に収束する (SQLite一時DBで部分index+ON CONFLICT述語)。
2. `fix_races_2025_dups.py` の分類ロジック: 合成した「別2頭が同スロット」データで、truth照合が
   正しい行を保持し誤りを是正対象に挙げる (DBなしの純ロジックテスト)。
3. race_num/place がNULLの行は部分インデックス・upsert述語の対象外になる。

## 受け入れ基準
- 新migration (部分ユニークインデックス) が用意され、重複残存時は中断する preflight を持つ。
- `fix_races_2025_dups.py` が dry-run で338組の分類・CSVバックアップを出力し、`--apply` は
  ガード付きトランザクション+検証commit。**本番未実行** (実行は上位モデルが承認後)。
- updater の ON CONFLICT が部分インデックスに整合し、SQLite/PGでテストパス。
- 全テストパス。本番Neon・ability.db は無変更。

## やらないこと
- 全体 NOT NULL / 全体 UNIQUE の適用 (履歴117万NULL行に不可能)。
- 履歴 race_num の一括backfill (不要・スコープ外)。
- 本番Neonへの適用・338組の実是正 (SQL/スクリプト用意まで。実行は上位モデル承認後)。
- place=NULL 34,219行の是正 (部分インデックス述語外・別タスク)。
- jra_ev.py・スコアリング等、登録アプリ外への波及。
