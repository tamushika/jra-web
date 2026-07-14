# SPEC T34-fix: 登録アプリ (jra_db_updater.py) の重複防止

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T34。
背景・根本原因: [T34調査記録](../T34-neon-dedup-investigation.md)。
Neon側の誤行5組は **2026-07-14 に上位モデルが削除済み** (netkeiba正・バックアップ有)。
本SPECは**再発防止 (登録アプリの修正)**。

**Codexは実装と検証まで。採否・コミットは上位モデル。** `codex/T34` ブランチか未コミットで作業。

## 根本原因 (確定済み)
1. **主因**: 重複ガードが `(date, place, race_name)` で **race_num を欠く** (jra_db_updater.py L244)。
   race_name は `<h2>` 由来の不安定キーで、揺れ/空の際に別レースの登録がすり抜け重複する。
2. **誘発**: race_num 抽出が脆い正規表現 (L209) で、不一致時に黙って None。
3. **痕跡**: horse_odds は結果ページに列が無く NULL になりやすい (誤行の指紋)。
4. **副次**: ヘッダ→セルの位置マッピング (L174-175) が行長不一致で列ずれし得る。

## 要求仕様
1. **重複ガードを自然キー (date, place, race_num) に変更** (jra_db_updater.py `insert_into_db`):
   - race_num を含めて既存チェックする。
   - **race_num が特定できない URL は登録を拒否**する (None のまま INSERT しない。
     日本語エラーで「R番号を特定できませんでした」を返す)。
2. **race_num 抽出の堅牢化**: 現行の CNAME 正規表現に加え、URL の別パターン・本文の
   「11レース」「第11競走」等からのフォールバックを実装。抽出結果は 1..12 の範囲検証。
3. **Neon `races` に一意制約 + upsert**:
   - `UNIQUE (date, place, race_num, horse_number)` を張るマイグレーション (既存重複が
     無いこと前提。念のため作成前に重複チェックし、あれば中断して報告)。
   - INSERT を `ON CONFLICT (date, place, race_num, horse_number) DO UPDATE` にして、
     再登録は**上書き**にする (二重行を作らない)。pandas `to_sql` では制御できないため、
     明示的な upsert SQL (executemany / execute_values) に置き換える。
4. **ヘッダ↔セルのマッピングを見出しテキスト基準に**する (位置インデックス依存をやめる)。
   セル数と見出し数が食い違う行は検出してスキップ・警告 (取消/除外馬の行崩れ対策)。
5. horse_odds は結果ページで取得できないことが多い事実を踏まえ、**取得できない場合は
   明示的に NULL** とし (現状踏襲)、オッズ充填は backfill_odds_netkeiba.py に委ねる旨を
   コメントで明記 (誤解防止)。

## テスト (最低3件)
1. race_num が抽出できない URL/本文で `insert_into_db` が拒否エラーを返す。
2. race_num を含む重複ガードが、同一 (date,place,race_num) の再登録を弾く/上書きする
   (upsert 経路のユニット。DBはモック or 一時SQLite)。
3. 見出し↔セルのマッピングが、行長不一致の行で列ずれせずスキップされる。

## 受け入れ基準
- 同一レースを2回登録しても Neon に重複行が増えない (upsert)。
- race_num 不明URLは登録されない。
- 既存の正常系登録が壊れない (回帰)。検証手順と結果を報告。

## やらないこと
- ability.db / runs 側の操作 (T32/T34 で修正済み)。
- 既存の Neon データの再クレンジング (削除は完了済み)。
- EV通知・スコアリング等、登録アプリ外への波及変更。
