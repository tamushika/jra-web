# T34 調査記録: Neon `races` 重複行と登録アプリの誤結合

調査: 2026-07-14 (Opus 上位モデル)。**破壊的操作 (Neon削除) はユーザー承認待ちのまま未実施**。
本書はコード静的解析による根本原因の特定と、確認用クエリ・修正方針の提案。

## 結論 (先に)

重複の**発生源は Neon `races` テーブル**であり、書き込み元は **`jra_db_updater.py` の1経路のみ**。
`extend_ability_from_neon.py` は Neon `races` を**1:1でそのまま `runs` にコピー**しており
(馬番JOINは無い)、`runs` の重複は「Neon の重複がそのまま流入した」もの
(garbage in → garbage out)。したがって修正すべきは登録アプリ側。

## 経路の確認

- `extend_ability_from_neon.py` L62-70: `SELECT … FROM races WHERE "馬名" IS NOT NULL AND
  date > … AND race_name NOT LIKE '%障%'` の結果を、L107-122 で行ごとに `batch` へ積み、
  L135-138 で対象期間を DELETE してから INSERT。**行の結合・集約は無い**。Neon に同一
  (date, place, race_num, horse_number) が2行あれば `runs` にも2行入る。→ 結合バグは無く、
  上流の重複を忠実にコピーしているだけ。

## 根本原因 (jra_db_updater.py・コードから証明可能)

### 原因1【主因・構造的】重複ガードの自然キーが弱い (race_num 欠落)
`insert_into_db` L244-245:
```
SELECT COUNT(*) FROM races WHERE date=:d AND place=:p AND race_name=:rn
```
- ガードが **(date, place, race_name)** で、**race_num (R番号) を含まない**。
- `race_name` は `<h2>` のスクレイプ値で、空になったり表記が揺れたりし得る**不安定なキー**。
  同一レースを2回登録した際に race_name が僅かに異なると (あるいは両方空だと)、ガードを
  すり抜けて**2セット目が INSERT され重複**する。レースの自然キーは本来 (date, place, race_num)。

### 原因2【誘発】race_num 抽出が脆く、失敗時に黙って None
L209 `re.search(r'pw01sde\d{2}\d{2}\d{4}\d{4}(\d{2})20\d{6}', url)`:
- URL の CNAME 形式がこの正規表現に一致しないと **race_num_val = None**。
- race_num が None のまま Neon に入ると、(date, place) だけで並ぶ複数レースが下流
  (`runs`) の (date, place, r, umaban) 空間で衝突しやすくなる。**「馬番キーの誤結合疑い」の
  正体は、SQLの明示JOINではなく「race_num を欠いた弱い自然キーによるレース取り違え」**。

### 原因3【符合する痕跡】horse_odds が更新アプリでは基本 NULL
- L268 `horse_odds_raw = (r.get("単勝") or r.get("単勝オッズ") or r.get("オッズ") or "")`。
  JRA **結果ページ**には確定オッズがこの列名で無いことが多く、`horse_odds_val` は**NULLに
  なりやすい** (extend_ability L101 のコメントも「horse_odds は backfill_odds_netkeiba.py /
  将来の updater 改修で充填」と明記)。
- T34 の手がかり「**horse_odds 列だけ両行同一**」は、**両行とも NULL** で一致している、と読める。
  他の成績列 (rank/time/jockey 等) は取り違えた別内容なので食い違い、更新アプリ由来で唯一
  常時 NULL の horse_odds だけが一致する — という**誤登録行の指紋**と符合する
  (netkeiba backfill 由来の行はオッズが入るため、この指紋は「updater 由来の重複」を指す)。

### 原因4【副次】ヘッダ→セルの位置マッピングが脆い
L174-175:
```
base_cols = [c for c in columns if c not in ("単勝配当","複勝配当")]
row = {base_cols[i]: cells[i] for i in range(min(len(base_cols), len(cells)))}
```
- 見出し数とセル数が食い違う行 (取消・除外馬、rowspan/colspan、注記行) があると**列がずれ**、
  値が別列に入り得る。「馬名は正しいが成績が対応しない」症状の二次的な発生源になり得る。

## 修正方針 (別タスク化・ユーザー承認後)

**A. 予防 (登録アプリ)** — Codex 実装向け SPEC を別途起票:
1. 重複ガードを **(date, place, race_num)** に変更。race_num が特定できない URL は**登録拒否**
   (黙って None で入れない)。
2. Neon `races` に **UNIQUE(date, place, race_num, horse_number)** 制約を張り、
   `INSERT … ON CONFLICT DO UPDATE` (再登録は上書き) にする。
3. race_num 抽出の堅牢化 (URL 複数パターン + 本文 R番号のフォールバック)。
4. ヘッダ↔セルは位置ではなく**見出しテキスト照合**にし、行長不一致を検出・スキップ。

**B. 是正 (一度きりのクリーンアップ)** — **ユーザー承認 + バックアップ必須**:
- Neon `races` の 2026-07-04 誤行を削除 (T32 と同じく **netkeiba を正**とする)。
- 削除前に該当行を CSV バックアップ。ability.db 側は T32 で修正済みだが、**Neon 修正前に
  `extend_ability_from_neon.py` を再実行すると重複が復活する**ため、Neon 是正が先。

## 確認用クエリ (読み取り専用・jra-runner か人手で)

Neon で 2026-07-04 の重複と、どの列が食い違い/一致するかを実データで確認する:
```sql
SELECT date, place, race_num, horse_number, "馬名", rank, jockey, time, horse_odds
FROM races
WHERE date = '260704'
  AND (place, race_num, horse_number) IN (
    SELECT place, race_num, horse_number FROM races
    WHERE date = '260704'
    GROUP BY place, race_num, horse_number
    HAVING COUNT(*) > 1)
ORDER BY place, race_num, horse_number, "馬名";
```
- 期待: 各グループで horse_odds が両行 NULL (原因3の裏取り)、rank/jockey/time が食い違う。
- race_num が NULL のグループがあれば原因2の裏取り。
- 5組の実体 (どちらが netkeiba と一致するか) を確定してから B の削除対象を決める。

## 2026-07-14 追記: migration preflight で設計是正が必要と判明

当初の migration (`race_num SET NOT NULL` + 全体 `UNIQUE`) を本番Neonへ適用する前に
READ-ONLY の preflight を実施した結果、**当初設計は実データに適用不能**と判明:

| 項目 | 値 |
|---|---|
| `races` 総行数 | 1,232,092 (2001-2025を含む全履歴。想定した「recentな結果表」ではない) |
| **race_num が NULL** | **1,178,074 (95.6%)**。race_num充填は2025以降のみ (2025: 44,634 / 2026: 9,384) |
| place が NULL | 34,219 |
| **非NULLキーの重複** | **338グループ (676行)・全て2025年**。7/4の5組以外 |

- `race_num SET NOT NULL` は95.6%がNULLの履歴テーブルに不可能 → **全体UNIQUEは断念**。
- 2025年338組の重複は7/4と症状が異なり、同一 (place,race_num,horse_number) に**別々の2頭**
  = **race_numの誤付与** (単純な誤行削除では直らず、正しいrace_num判定が要る)。
- **連鎖問題**: コミット済updaterの `ON CONFLICT (自然キー)` は対応制約が未作成のため、
  今のNeonに新updaterで登録するとinsertが失敗する。制約作成が前提。

**是正方針** → [SPEC-T34b](codex/SPEC-T34b-partial-unique-index.md):
1. 全体UNIQUE → **部分ユニークインデックス** (`WHERE race_num IS NOT NULL AND place IS NOT NULL`)
   で履歴117万NULL行に触れず今後の登録行だけ保護。
2. 2025年338組の是正スクリプト (ability.db 2025 truth 照合・dry-run/バックアップ/承認後apply)。
3. updater の ON CONFLICT を部分インデックス述語に整合。

## 状態
- 根本原因 = 特定済み。7/4の5組は是正済み (Neon)。登録アプリ再発防止コード = コミット済 (7e5ce59)。
- **migration は未適用** (当初設計が実データに不適合と判明 → SPEC-T34b で再設計)。
- **未実施 (承認待ち)**: SPEC-T34b の Codex実装レビュー、2025年338組の是正実行 (承認後)、
  部分ユニークインデックスの本番適用。
