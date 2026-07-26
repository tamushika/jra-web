# T64 週次DB登録の日曜失敗・恒久修正

仕様: [SPEC-T64](codex/SPEC-T64-weekly-ingest-sunday-fix.md)  
実装日: 2026-07-26

## 実装結果

親リポジトリの `jra_app.py` と `batch_register.py` を修正した。

- 全JRA GET（チェックサム総当たり内部を除く）を共通403 retryへ統一。
  403時は8秒×attempt、最大6回。403以外は即時返却する
- チェックサム総当たりは各256要求へretryを掛けず、全体空振り時だけ60秒後に
  もう1周する
- `discover_weekend_races` はHTTP非200と「HTTP 200・リンク0件」を区別する
- Step 2.5として、14日以内の保存済みaccessSシードをaccessD CNAMEへ変換し、
  出馬表をチェックサム解決してページ内のレース結果accessSリンクを辿る
- 14日超の古いシードはStep 2.5／従来Step 3の両方で使用しない
- 成功した週次実行だけが、全登録終了後に必ず会場別シードを更新する
- ローカル実行時は `outputs/weekly_register_history.log` へ成否・登録数を1行追記。
  GitHub Actionsではログファイルを作らず、従来どおりexit codeを返す

## 上位モデルレビューでのハードニング (2026-07-26, Fable 5)

Step 2.5 が出馬表ページの「最初のaccessSリンク」を無条件に採用していた。出馬表には
過去重賞のaccessSリンクが多数並ぶ (7/26実測46本) ため、過去週のシードを掴むと
「その週を全SKIPで再登録して成功扱い→古いシードを保存」という無症状の失敗になる。
**CNAMEに対象日 (YYYYMMDD) を含むリンクのみ採用**するよう上位モデルが直接修正し、
過去リンク混在・対象日リンク不在の両ケースの回帰テストを追加した (計12テスト)。

## GitHub Actions確認

`.github/workflows/weekly_db_update.yml` の実行stepは
`run: python batch_register.py` であり、`continue-on-error` は存在しない。
`batch_register.py` のreturn 1は `sys.exit(main())` を介してジョブfailへ伝播する。
workflow変更は不要だった。

## テスト

親リポジトリに `test_t64_weekly_ingest.py` を追加し、以下をfixtureで保証した。

- 403→403→200と待機列8秒・16秒（共通ラッパー単体およびdiscover経由）
- HTTP 403とHTTP 200リンク0件の区別
- 総当たり全体の1回だけの再試行
- 14日超シードの拒否
- 日曜トップ空振りからStep 2.5成功
- accessSシード→accessD CNAME変換と結果リンク追跡
- 成功時の末尾seed保存、失敗時の非保存、exit codeとローカル履歴

専用テストは11件すべて通過した。

JRA・netkeibaへの実通信、Neon書き込み、workflow実行は行っていない。
