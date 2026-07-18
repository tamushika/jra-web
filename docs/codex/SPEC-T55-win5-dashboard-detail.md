# SPEC T55: 実績ダッシュボードのWIN5詳細表示 (買い目×人気・勝ち馬・払戻金額)

共通指示: [README.md](README.md) 参照。タスク管理: `docs/TASKS.md` の T55。
起票: 2026-07-18 上位モデル (ユーザー要望)。

**実装者の担当は実装・テスト・数値報告まで。採否・コミットは上位モデル。**

## 1. 要望 (ユーザー 2026-07-18)

実績ダッシュボードのWIN5成績で詳細を確認したい:
1. 5レースの買い目と、各馬の人気を記載する。不的中レースは
   「何番の何番人気が勝ったのか」も記載する。
2. WIN5の払戻金額も記載する。

## 2. 現状とデータ

- `jra_perf.py` の `collect()` win5節は 案ごとの hit_flags (x/5) のみ。
  買い目は `win5_predictions.selections_json` (`horse_numbers` per race_id) に既存。
- **人気は確定オッズから導出する**: `race_results.final_win_odds` をレース内で
  昇順順位化 (最小=1番人気、同値はmin rank)。人気列は logging DB に存在しない。
  final_win_odds が無い馬は「-」表示 (推測しない)。
- 勝ち馬 = `race_results.finish_position == 1` (既存 `_win5_hit_flags` と同じ)。
  勝ち馬の馬番・馬名・人気を不的中レースに表示する。
- **WIN5払戻金額はどこにも無い** (logging DBにも ability.db `race_payouts`
  [単勝〜三連単のみ] にも)。新規取得・保存が必要。

## 3. 実装内容

### 3.1 WIN5結果の取得・保存 (新規・小規模)

- logging DB に migration で `win5_results` テーブルを追加
  (既存 `schema_migrations` の作法に従う):
  `race_date (PK)`, `payout_yen`, `hit_ticket_count`, `carryover_flag`,
  `carryover_amount`, `winning_numbers_json`, `fetched_at`, `source_url`,
  `source_hash`。
- 取得元は netkeiba または JRA公式のWIN5結果ページ (実装時に安定な方を選定し
  source_url に記録)。**1開催日=1リクエスト・1秒スリープ厳守**。
  取得失敗はfail-soft (テーブル未登録のまま、ダッシュボードは「未取得」表示)。
- 呼び出しは既存の結果取り込み経路 (`result_service` の日次結果取得) に載せる。
  ダッシュボード (読み取り専用) からは**取得を起動しない**。
  過去日の埋め戻しは同経路の日付指定再取得で行えること (2026-07-13以降の
  ログ蓄積分が対象。全埋め戻しはリクエスト数十件程度)。
- `winning_numbers_json` は照合用の冗長記録。表示の勝ち馬判定は従来どおり
  `race_results` を正とし、両者が不一致の場合はダッシュボードに警告表示。

### 3.2 ダッシュボード表示 (jra_perf.py collect + テンプレート)

- WIN5実績 (案ごと) と日別一覧のWIN5欄に展開ボタン (または常時展開) で
  詳細ブロックを追加:
  - レースごとに1行: `会場R (レース名) | 買い目: 8(2人気) 12(5人気) … | ○/×`。
    的中選択馬 (勝ち馬と一致した馬番) を強調表示。
  - 不的中レースは `→ 勝ち馬: 7番 ムスタング (9番人気)` を併記。
  - 未確定レースは `未確定`。
  - 最下行: `WIN5結果: 的中x/5 | 配当 ¥xx,xxx,xxx (的中n票) | キャリーオーバー有無`。
    自案的中時は `払戻 ¥…` を強調 (購入1票=100円前提で配当額そのまま)。
    win5_results 未取得時は `配当: 未取得`。
- 表示は既存のUIパターン (T30日別一覧) に合わせる。JST表示規約 (_to_jst) 維持。

## 4. 制約 (不変条件)

- `jra_ev.py`・`jra_win5.py`・通知条件は一切変更しない。
- ダッシュボードは読み取り専用のまま (書き込みは result_service 経路のみ)。
- 5004単体 (`jra_perf.py`) と suite (`jra_suite.py` Blueprint) の両方で
  動作すること。**suiteの再起動はレース日に行わない** (5004単体は再起動可)。
- HTMLテンプレートはCRLF規約 (`.replace("\r\n","\n")` で読み `newline="\n"` で書く)。

## 5. テスト

1. 人気導出: final_win_odds 昇順→人気、同値tie=min rank、欠損「-」
2. 的中/不的中/未確定の3状態の表示データ生成 (合成fixture)
3. win5_results 有/無/勝ち馬不一致の表示分岐
4. WIN5結果ページのパースfixture (的中・キャリーオーバー日の実HTML断片)
5. migration が既存DBに冪等適用できること
6. `python -m pytest tests/ -q` 全体パス

## 6. 成果物

変更diff + テスト + 実データでの表示確認 (直近の的中日 7/12 [2/5] と
最新プランでのスクリーンショット相当のtext dump)。
