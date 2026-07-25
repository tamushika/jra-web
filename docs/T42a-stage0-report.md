# T42a Stage 0 / バックフィル運用報告

仕様: [SPEC-T42a](codex/SPEC-T42a-training-data-trial-cache.md)  
実装日: 2026-07-25  
Stage 0実通信: **未実施**（トライアル登録前のため）

## 1. 登録前の実装結果

`t42_training_cache.py` に次を実装した。

- `data/t42/raw/{race,horse,race_index}/<key>.html` のimmutable raw HTML
  キャッシュと `data/t42/manifest.sqlite`
- SHA-256照合、孤立ファイル／上書き拒否、キャッシュ再実行時HTTP 0件
- `Training_Day`・`TrainingTimeDataList`・馬ID coverage、および会員登録誘導DOMを
  組み合わせたfail-closed検査。検査を通る前はファイルを作らない
- 会員コンテンツ欠落3件連続でセッション更新要求、全失敗5件連続で停止
- 1.05秒間隔、JST日次8,000リクエスト上限（retryも1件として記録）
- T54c／週次更新と同じ `data/t54c/scrape.lock`
- `--live --acknowledge-trial-active` の二重ゲート。どちらかがなければ通信しない
- race_id解決用日別一覧もimmutableキャッシュし、同じ開催日を二度取得しない
- Phase 2/4の馬IDは、名前結合ではなくPhase 1/3で取得した会員ページのhorse_idから
  列挙する。これにより `nk_horse_ids` の過去年不足に依存しない

キャッシュ、manifest、クッキーは `.gitignore` の `data/t42/` で一括除外した。

## 2. クッキーファイル

ユーザーがログイン済みブラウザの開発者ツール（Application / Storage →
Cookies）から、netkeibaのログイン維持に必要なcookieを確認し、次の形式で
`data/t42/netkeiba_cookies.json` に保存する。値はこのファイル以外へ貼らない。

```json
[
  {
    "name": "cookie-name",
    "value": "cookie-value",
    "domain": ".netkeiba.com",
    "path": "/"
  }
]
```

ブラウザ拡張等が `{"cookies": [...]}` 形式で出力したJSONも受け付ける。
ログ、summary、manifest、例外にはcookie名・値を出力しない。クッキーは最初の
ネットワーク要求の直前にだけ読むため、status表示とキャッシュ再パースには不要。

## 3. Stage 0 手順（登録後、100リクエスト以内）

最初は既知の3年代のレースページと、対応する馬ページだけを指定する。以下は
最大6リクエストであり、大量バックフィルを開始しない。

```powershell
python t42_training_cache.py --stage0 --live --acknowledge-trial-active `
  --target race:202603020312:16 `
  --target race:202106010112:13 `
  --target race:201805021210:18 `
  --target horse:2021106347 `
  --target horse:2015104202 `
  --target horse:2014105949 `
  --summary outputs/t42a_stage0.json
```

2018年のrace_idは取得前にブラウザ表示と照合し、異なる場合は正しいIDへ置換する。
実行後、raw HTMLをオフラインで確認し、以下を本報告へ追記する。

| 判定 | 結果（実行後記入） |
|---|---|
| トライアルで全頭の生時計・ラップが見える | 未判定 |
| 2021年・2018年まで遡れる | 未判定 |
| レース別ページの完全性 | 未判定 |
| 馬別ページの完全性／URL契約 | 未判定 |
| 主力ページ種別 | 未判定 |
| 公開時刻を示す情報 | 未判定 |
| パーサー主要フィールド抽出率 | 未判定 |

1件でも会員登録誘導ページになる、または全出走馬のhorse_id coverageを満たさない
場合は保存されない。3件連続なら自動停止する。ティア不適合なら全量実行せず、
即時報告する。

## 4. 全量コマンド（Stage 0のGo裁定後のみ）

```powershell
# 優先1: 2021-2026H1 レース別
python t42_training_cache.py --phases 1 --live --acknowledge-trial-active `
  --summary outputs/t42a_phase1.json

# 優先2: Phase 1キャッシュから馬IDを列挙
python t42_training_cache.py --phases 2 --live --acknowledge-trial-active `
  --summary outputs/t42a_phase2.json

# 優先3/4: 残枠があり、上位モデルが明示的にGoした場合だけ
python t42_training_cache.py --phases 3 --live --acknowledge-trial-active `
  --summary outputs/t42a_phase3.json
python t42_training_cache.py --phases 4 --live --acknowledge-trial-active `
  --summary outputs/t42a_phase4.json
```

日次上限に達した実行は安全に止まり、翌日の同じコマンドでキャッシュ済みを
HTTP 0件で通過して続行する。進捗のみは `python t42_training_cache.py --status`。

## 5. 日次8,000上限と7日トライアルの整合性

仕様の記載量（約7.6万ページ）を8,000件/日で取得するには、単純計算で最低10暦日
かかる。race_id解決用の開催日一覧も約590件必要であり、**7日間で4フェーズ全量は
両立しない**。

現DB実測（2026-07-25）はレース18,905件、既存馬ID 32,391件、旧期間レース
10,362件。ただし馬IDは会員レースページから再列挙するため、最終件数は取得後に
確定する。

| 日 | 上限内の優先作業 |
|---|---|
| 7/26 | Stage 0のみ（開催終了・週次取り込み前は大量取得しない） |
| 7/27–7/29 | Phase 1（開催日一覧を含め約19.5k） |
| 7/29–8/1 | Phase 2（約32.8k、日次残枠を連続利用） |
| 8/1 | 取り漏れ・SHA監査。Phase 3/4は残枠がある場合のみ |

Phase 1+2だけでも約52.3k件で、7/26をStage 0専用にすると残り6日間の上限48kを
超える。8/2 1:00までの端数時間を使うか、Stage 0で完全な方を主力1種に絞る必要が
ある。最終配分はStage 0直後に上位モデルが裁定する。

## 6. 日次進捗

| 日時(JST) | phase | cached | fetched | failure | requests/day | 備考 |
|---|---:|---:|---:|---:|---:|---|
| 2026-07-25 | 実装 | 0 | 0 | 0 | 0 | 登録前。外部通信なし |

## 6. 上位モデルレビュー・受理と運用裁定 (2026-07-25, Fable 5)

**受理**。18→19テスト+全体601パス。ログイン対応immutableキャッシュ・ペイウォール検知
fail-closed・クッキー隔離 (ログ/manifest/例外への非混入をテストで機械保証)・二重liveゲート・
共通排他ロック・日次上限・連続失敗停止を確認した。

### 上位モデルによるハードニング (直接修正)

`validate_member_content` は `.TrainingTimeDataList` の**存在**のみ確認し、中身の数値時計の
有無を見ていなかった。トライアル階層が「表の骨組みは見せるがタイム値をマスクする」挙動の場合、
無人バックフィル中に中身の空なページをimmutableキャッシュへ焼き込む恐れがある
(トライアル後は再取得不能)。**少なくとも1つのタイミングセルに数値 (`\d+\.\d`) があることを
要求する検査を追加**し、マスクページを `training_values_masked` として保存拒否する回帰テストを
足した (計19テスト)。

### 日次上限と7日の整合 — 運用裁定

Codexの報告どおり、Phase 1+2で約52.3k件は8,000/日で6.5-7日を要し、Phase 3/4まで含めると
7日に収まらない。裁定:

1. **Phase 3/4 (2018-2020) は既定で見送る**。凍結分割 (学習2021-23/選抜2024/テスト
   2025-26H1) は**2021年以降で完全にカバー**されるため、2018-2020ウォームアップの欠落は
   T42の核心 (調教特徴の予測価値) に影響しない。Phase 1+2完了後に明確な余剰がある場合のみ、
   上位モデルが個別にGoする
2. **Phase 1 (レース調教2021-26H1) を最優先**。レースページは「そのレース発走前に公開されて
   いた調教」= as-of的に正しい主系であり、件数も小さい (約19.5k)
3. **Phase 2 (馬別履歴2021-26H1) を次点**。追切本数・間隔・強度パターンには馬別全履歴が
   必要だが、馬ページは取得時点の全キャリアを含むため、特徴構築時に「対象レース以前のみ」で
   厳密に日付フィルタする規律が必須 (退会後の作業で担保)
4. **Phase 1と2の最終的な優先順は、Stage 0で「どちらに完全な生時計があるか」を見てから確定**
   する。判定不能なら両方 (Phase 1→2) を上記予算内で取得

### 7/26 1:00 の運用手順 (ユーザー)

1. 登録後、ログイン済みブラウザからcookieを `data/t42/netkeiba_cookies.json` へ (§2の形式)
2. Stage 0コマンド (§3、最大6リクエスト) を実行 → raw HTMLを目視 + `--summary` を上位モデルへ
3. 上位モデルがStage 0を裁定 (ティア適合なら主系確定+Phase順のGo、不適合なら**即解約**指示)
4. 7/26は開催日のため大量取得はしない。Phase 1は週次結果取り込み完了後 (7/26夜) 以降
5. 8/1中に解約 (課金発生前)
