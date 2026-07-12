# Codex 作業仕様書の置き場

上位モデル (Claude) が書いた仕様書を Codex に渡して実装させるためのディレクトリ。

## 運用フロー
1. 上位モデルが `SPEC-*.md` を書く (このディレクトリ)
2. 人手で Codex に仕様書の内容を渡す (ファイルパスを指定するか本文を貼り付け)
3. Codex は **`codex/<タスクID>` ブランチで作業するか、変更をコミットせず作業ツリーに残す** (main へ直接コミット・push しない)
4. 上位モデルが diff と検証結果をレビューし、問題なければ main にコミット
5. 完了したら `docs/TASKS.md` の状態を更新

## Codex への共通指示 (全仕様書に適用)
- リポジトリ: `c:\Users\owner\project\.venv\jra-web`。Python は `c:\Users\owner\project\jra-runtime\Scripts\python.exe`
- Windows環境。コンソール出力は `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` の流儀
- `.env` の秘密情報 (DATABASE_URL 等) を出力・コミットしない
- netkeiba へのアクセスは1リクエスト1秒以上のスリープ
- 本番挙動 (特にEV通知条件: LINE=5分前×EV>=1.3) を変えない
- 検証手順と結果を必ず報告に含める
