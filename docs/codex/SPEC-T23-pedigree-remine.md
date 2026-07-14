# SPEC T23: 血統条件を含む criteria 再マイニング

共通指示: [README.md](README.md) 参照。タスク管理: docs/TASKS.md の T23。
前提となる先行タスク: [SPEC-T15](SPEC-T15-remine-rules.md) (期間クリーン化・v2)。本タスクは
T15 の凍結分割・選抜規律をそのまま踏襲し、**血統条件 (父/母父/系統) を候補に加える**。

**Codexは実装と数値報告まで。採否・コミットは上位モデル。**
`codex/T23` ブランチか未コミットの作業ツリーで作業し、main へ直接 push しないこと。

---

## 背景と、これまでの経緯 (重要)

- `mine_criteria.py` は現状 **血統条件を一切採掘しない**。理由はコメントに残る
  「血統条件は horse_pedigree 蓄積完了後に追加予定 (現状DBに父が無い)」(mine_criteria.py L14)。
  この前提は **血統バックフィル完了で解消済み** (2026-07-11 sire_pts採用、T16実行時カバレッジ98.9%)。
- ただしデータを入れただけでは採掘に繋がっていない。**実際の技術的ブロッカーは2つ**:
  1. 採掘用の馬 dict を作る `backtest_criteria.build_h(cur, prev)` が
     **`"sire": "-", "bms": "-"` をハードコード**している (backtest_criteria.py L95)。
     ability.db の runs には父・母父が無いため。
  2. `mine_criteria` は述語評価で `analysis.check_condition(c, h, r_ctx, {}, mawari_map)` と
     **sire_lineage に空dict `{}` を渡している** (mine_criteria.py L129) ため、系統 (「〜系」)
     条件も評価できない。
- 一方、述語エンジン `analysis.check_condition` は **血統条件を評価できる** (analysis.py L487-500:
  「父が」「母父が」「父or母父が」「父・母父が」「〜系」「以外」を `h['sire']`/`h.get('bms')`/
  `sire_lineage` で判定)。つまり **`h` に父・母父を載せ、sire_lineage を渡せば動く**。
- 血統データの取り出し口: **`api/pedigree_store.load_all(use_cache=True)`** が
  `{馬名: {"sire":…, "bms":…}}` を返す (horse_pedigree テーブル: sire/bms/dam。ローカルキャッシュ
  経由でNeon非依存)。系統は `analysis.load_sire_lineage(base_dir)` が `syuboba.csv` から
  `{種牡馬名: 系統グループ}` を返す。
- **mined_rules の役割**: T33 で確定のとおり mined_rules は **Web手動スコア専用** (本番CL/betting
  モデルには無関係)。よって T23 は Web スコアの精度向上策であり、評価は T15 と同型
  (該当馬の複勝率・単複回収)。**購入判断・EV通知には一切関与しない**。
- **採否の勘所 (T24 の教訓)**: 書籍血統ルール一括解禁は「〜系以外」型が大半で非選択的、
  ◎の単勝回収が低下したため **不採用** (2026-07-13)。また連続量の **sire_pts は既に採用済み**で
  データ駆動の血統価値を捉えている。したがって本タスクの本質的な問いは
  **「マイニングした離散的な血統ルールは、既存の sire_pts を超える増分価値があるか」**。
  ベースライン比だけでなく **sire_pts 込みの現状に対する増分**で見ること。

---

## 要求仕様

### A. 採掘・評価パイプラインへ血統を接続 (enabling)
1. `mine_criteria.py` に血統ロードを追加する:
   - `pedigree_store.load_all(use_cache=True)` で `{馬名: {sire, bms}}` を取得。
   - `analysis.load_sire_lineage(API_DIR)` で系統マップを取得。
2. `build_h` で `"-"` になる sire/bms を、**採掘用に馬名で上書きする**。`mine_course` と
   `evaluate_rules` の両方で、各 `h` に対し `ped = pedigree.get(cur["horse"])` を引いて
   `h["sire"] = ped["sire"]`, `h["bms"] = ped["bms"]` を設定 (欠損時は "-" のまま)。
   `build_h` 本体を変えるより、mine_criteria 側で「build_h の直後に上書きするヘルパ」を
   通す方が影響範囲が小さい (backtest_criteria の他利用者を壊さない)。どちらでも可だが
   **既存の非血統パス (T15/backtest_criteria) の挙動を変えないこと**。
3. `check_condition` 呼び出しの第4引数 `{}` を、ロードした **sire_lineage に差し替える**
   (mine_course の候補ベクトル生成 L129、evaluate_rules L402 の両方)。
4. **血統カバレッジを報告**する (何頭中何頭に父/母父が付いたか、%)。欠損馬は条件不成立
   (False) として扱い、例外にしない。

### B. 血統候補条件の追加
`candidate_conditions()` に、そのコースの発見期間で頻度の高い血統属性を候補として足す
(既存の非血統候補は残す):
- **父**: 発見期間で騎乗数ならぬ「出走数」上位N (N=8目安、各 `n >= MIN_DISCOVER_N`) の種牡馬について
  `"父が<種牡馬名>"`。
- **母父**: 同様に上位N の母父について `"母父が<母父名>"`。
- **系統**: そのコースに出現する系統グループ (syuboba) について `"父が<系統>系"`
  (check_condition の系統マッチを使う)。件数下限 `MIN_DISCOVER_N` を満たすもののみ。
- 探索段数・ビーム幅・収縮/選抜の閾値 (`DISCOVER_LIFT_*`, `SELECT_LIFT`, `SHRINK_N0`, `BEAM`,
  `MAX_RULES`) は **T15 と同一に据え置く** (血統の効果だけを測るため、パラメータは弄らない)。
- 「〜系以外」型の広すぎる条件 (発見期間該当率>=0.97) は既存の無差別除外ロジック
  (mine_criteria L136) が自動で落とすはず。落ちない場合は報告する。

### C. 分割と出力 (T15 と同じ規律)
1. 分割は **発見 2021-2023 / 選抜 2024 / 固定テスト 2025 / 追加確認 2026H1**。
   `validate_windows` の不変条件 (時系列・非重複・2025以降を選抜に使わない) を維持。
2. 出力は **`mined_rules_v3.csv`** (血統込み)。**本番 `mined_rules.csv` も v2 も上書きしない**
   (`write_rules` の本番上書きガードを維持)。
3. 血統の**増分**を切り分けるため、可能なら2系統を出力・評価する:
   - (a) 非血統のみ (= T15 v2 相当) と (b) 血統込み (v3)。
   同一ハーネスで両者を並べ、**血統込みが v2 に上乗せする分**を見えるようにする。

### D. 評価 — sire_pts に対する増分が主眼
`print_evaluation`/`evaluate_rules` を流用し、2025 と 2026H1 (真の比較は 2026H1) で報告:
1. ルールセット別 (現行リークあり / v2クリーン / v3血統込み) × 種別 (買い/消し) の
   ルール本数・該当n・複勝率・単回収・複回収。
2. **sire_pts 層別**: 血統ルール該当馬を sire_pts の高/中/低で層別し、各層で
   「血統ルール該当馬 vs 同層の非該当馬」の複勝率差を出す。**sire_pts を揃えても
   なお血統ルールが上乗せするか**を判定する材料。
3. 血統ルール該当馬と「sire_pts 上位馬」の**重複率** (血統ルールが sire_pts と冗長でないか)。
4. 個別ルールの安定性: 発見→選抜→固定テストで方向が一致するルールの割合。

## テスト (最低4件)
1. `pedigree_store.load_all` 由来の sire/bms が既知の1頭に正しく付く (合成 or 固定馬名)。
2. `check_condition` が sire_lineage を渡したとき「父が<系統>系」を正しく判定する (合成 h)。
3. `validate_windows` の不変条件が維持され、2025以降を選抜に使うとエラー。
4. `write_rules` が本番 `mined_rules.csv` を上書きしようとするとエラー (v2/v3 は書ける)。
5. (可能なら) 血統欠損馬で条件が例外でなく False になる。

## 受け入れ基準
- `mined_rules_v3.csv` が生成され、本番CSV・v2 CSV が不変。
- 血統カバレッジと、ルールセット別 (現行/v2/v3) × 2期間の比較表、sire_pts 層別の増分表。
- 再実行可能 (同じコマンド・同じ乱数なしで同じ結果)。既存テストが壊れない。

## やらないこと
- 本番 `mined_rules.csv` / `criteria_weights.json` の差し替え (採否判断後の別作業)。
- 本番CL/betting モデル・EV通知への接続 (mined_rules は Web手動スコア専用)。
- 選抜基準パラメータのチューニング (血統の純効果を測るため据え置き)。
- 血統の**再スクレイプ** (netkeiba 叩き直し禁止。`pedigree_store` のキャッシュ/既存DBを使う)。
- 固定テスト (2025/2026H1) を候補生成・選抜に使う (リーク厳禁)。
