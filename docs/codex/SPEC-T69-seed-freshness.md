# SPEC-T69: 週次登録シードの鮮度維持 (T64フォロー)

作成: 2026-08-16 (Fable 5)。実装: Fable 5 直接 (ユーザー指示 2026-08-16)。
対象リポジトリ: **親 (jra-tools)** — `jra_app.py` / `batch_register.py` /
`.github/workflows/weekly_db_update.yml` / `last_seed_urls.json` / テスト。

## 1. 背景 (2026-08-16 障害の根本原因)

週次登録は3経路: Step 1 (JRAトップのリンク) → Step 2.5 (保存シード→出馬表→結果リンク、
T64) → Step 3 (シードから日付オフセット総当たり)。Step 2.5/3 のシードは
`last_seed_urls.json` で、`batch_register.py` は**成功終了時に必ず更新する**が:

- **GitHub Actionsランナー上の更新はrunの終了とともに破棄される** (ファイルはgit管理
  だがコミットバックする工程が無い)。Actionsは常に「最後に人がコミットしたシード」で走る
- シードには14日鮮度ガードがある (`_seed_candidates`、古いシードから開催回/日次を
  推定できないため)。コミット済みシードが7/25のまま8/16に22日超で失効し、
  当夜はトップリンクも0件だったため3経路全滅した (8/2・8/9はStep 1で成功していた
  ためこの劣化が露見しなかった)
- 失効時のログは「試行中...」の後に汎用エラーのみで、シード失効が原因だと
  即座に分からなかった

## 2. 修正内容 (3点)

### 2.1 ワークフローにシードのコミットバックを追加 (本命)

`.github/workflows/weekly_db_update.yml`:
- トップレベルに `permissions: contents: write` を追加
- `python batch_register.py` 成功後のstepで、`last_seed_urls.json` に差分がある場合のみ
  github-actions[bot] 名義でコミットし `git pull --rebase` → `git push`
- 差分なしなら何もしない (毎週の空コミットを作らない)

これにより **土曜のActions成功 (Step 1が通る) が日曜の保険 (Step 2.5) を自動補給する**
ループが成立する。シード年齢は定常で最大7日に保たれる。

### 2.2 シード失効の明示警告 (診断性)

`jra_app.py` に純関数 `seed_staleness_warning(seed_urls, target_date) -> str | None` を
新設し、`get_latest_race_url()` の Step 2.5 直前で呼ぶ。全シードが14日超 (または
日付抽出不能) なら最新シード日付入りの `[WARN]` を print する。判定ロジックは
`_seed_candidates` の鮮度条件 (0 <= delta_days <= 14) と同一定義。

### 2.3 現在の新シードをコミット (初期値)

2026-08-16夜の復旧で注入した3会場シード (20260816) を親リポジトリにコミットし、
次回Actionsが健全なシードで開始できるようにする。

## 3. 見送り (理由つき)

- **ローカル限定Step 2.7 (jra-webロギングDBの monitored_races からシード自動導出)**:
  8/16の手動復旧で有効性は実証済みだが、親アプリ→jra-webロギングDBの新たな結合を
  データ書き込み経路に増やす。2.1でシードが常時新鮮なら発動機会がほぼ無いため見送り。
  手動手順はTASKS.md 2026-08-16完了ログに記録済み (再発時はそれに従う)
- **checksum総当たりの完全シードレス化**: 開催回/日次をNeonから推定する案。
  精度とリクエスト数のトレードオフが大きく、2.1で十分なため見送り

## 4. 禁止事項

- `insert_into_db` / DB書き込みロジックの変更禁止
- Step 1〜3 の探索順序・鮮度ガード14日の変更禁止
- ワークフローのコミット対象は `last_seed_urls.json` のみ (他ファイルを含めない)

## 5. テスト (親リポジトリ、test_t64_weekly_ingest.py の様式)

1. `seed_staleness_warning`: 14日以内のシードあり → None / 全て15日超 → 最新日付を
   含む警告 / 空リスト・日付抽出不能 → 警告 (シード無し扱い) / 境界 (ちょうど14日) → None
2. ワークフローYAML: `permissions: contents: write`・シードコミットstep・
   対象ファイルが `last_seed_urls.json` のみであることの文字列検証
3. 既存テスト (T64含む) 全パス

## 6. 完了条件

- 親リポジトリのテスト全パス (`python -m pytest test_t64_weekly_ingest.py` + 新規)
- 実地検証は次回Actions実行 (土曜19:00) で「シードコミットが積まれる」ことを確認し
  TASKS.mdに1行 (日曜はそのシードでStep 2.5が機能するかを観察)
