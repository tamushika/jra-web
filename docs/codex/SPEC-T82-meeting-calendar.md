# SPEC-T82: レース詳細に「開催カレンダー」(開幕週・使用コース・天候/降水・馬場状態) を表示する

起票: 2026-09-25 (Fable 5.1)。ユーザー要望: 「レース詳細の所で、開幕何週目か、どの週で A コース B コースを使用したか、その週は雨が降ったかなどを一覧で分かるようにしてほしい」。

## 0. 調査済みの事実 (2026-09-25)
- **開催の同定**: Neon `races.kaisai` は `2026年9月20日（日曜）4回中山6日 10レース` 形式 (回次・場・日目)。当日出馬表 (`api/index.py` の `r_info`/`kaisai`) も同形式。`past_data_service.get_db_connection(base_dir)` で Neon (無ければローカル sqlite) に接続でき、`date` は `YYMMDD`、`condition` は 良/稍/重/不、`track_type` は 芝/ダート。
- **使用コース (A/B/C/D)**:
  - 今週分: JRA 馬場情報ページ `https://www.jra.go.jp/keiba/baba/` の「芝丈・使用コース・芝の様子」節 `<h3>使用コース</h3>` の直後の content に `C コース（Aコースから6メートル外に内柵を設置）`、「芝の状態」に `今週からCコースを使用します…` の文章。h2 に `第4回中山競馬第7日（2026年9月22日（火曜））` がある。**複数場開催時のページ構造 (場ごとのタブ/セクション) は実装時に確認**する (`api/index.py:fetch_baba_info` は `_data_cushion.html` / `_data_moist.html` の `div[title=場名]` を使っている。同じ規約で場ごとの節が取れる可能性が高い)。
  - 過去開催分: JRA 馬場情報アーカイブ PDF `https://www.jra.go.jp/keiba/baba/archive/<YYYY>pdf/<venue_en><NN>.pdf` (NN = 回次 2 桁。T50 の `data/t50/raw/2026/*.pdf` と同一) に **日別に 日付・曜日・使用コース (A/B/C)・クッション値・含水率・第N日** が並ぶ (nakayama03.pdf で確認: `3月27日 / 金曜日 / A / 10:30 / 9.1 / 10:30 / 13.5 13.2 13.1 13.5 / 第 1日`)。**進行中の開催は PDF が未公開** (nakayama04.pdf は 2026-09-24 時点 404)。
  - したがって進行中開催の「過去週の使用コース」は JRA から後追い取得できない。→ **毎週 (開催日の解析時) に馬場情報ページの使用コースを自前で保存**して蓄積する。記録開始前の週は「記録なし」と表示する。
- **天候・降水**: Open-Meteo 過去データ API `https://archive-api.open-meteo.com/v1/archive?latitude=..&longitude=..&start_date=..&end_date=..&daily=precipitation_sum,precipitation_hours,weather_code&timezone=Asia/Tokyo` (無料・キー不要・確認済み: 中山 9/12〜9/21 で 9/20 76.9mm, 9/21 153mm)。当日/翌日は forecast API (`api.open-meteo.com/v1/forecast` に `daily=` 同項目)。座標は `script.js` の `COURSE_DIRECTION` と同じ (`win5_predictor/venue_info.py` にも同表)。
- **馬場状態 (日別)**: Neon `races` の `date × place × track_type` の `condition` (最頻値)。当日は出馬表の `official_baba` (天候・芝・ダ)。
- **含水率・クッション値**: 当日は `fetch_baba_info`、過去は `data/t50/track_measurements.sqlite` (T50、`date, venue, cushion, turf_moisture_goal/4c, dirt_moisture_goal/4c`。7 月までの取得分) — 表示できる範囲で併記 (必須ではない)。

## 1. 設計
1. **ローカル蓄積テーブル** `meeting_course_usage` (ロギング DB `data/jra_logging.db`、migration 追加): `venue, week_start (YYYYMMDD 土曜)`, `meeting_no`, `course` (A/B/C/D/不明), `note` (「今週からCコース…」の文), `source_url`, `fetched_at`。同一 venue×week_start は上書きしない (append-only、初回のみ)。
2. **取得ジョブ**: 統合版の解析開始 (`jra_ev.worker_analyze_all` 完了時) と `jra_suite` 起動時に、当日開催の各場について JRA 馬場情報ページから使用コースを 1 回取得して保存 (失敗は警告ログのみ、解析を止めない)。**通知関数・解析ロジックは不変**。手動再取得用 CLI `python fetch_course_usage.py [--date YYYYMMDD]` も用意 (アーカイブ PDF がある過去開催は `--archive <YYYY> <venue_en> <NN>` で日別に取り込める)。
3. **API** (統合版のみ、本番 Web 不変): `GET /race/api/meeting_calendar?venue=中山&date=YYYYMMDD` を T73b と同じ `before_request` 短絡で `jra_suite.py` に実装 (`api/index.py` は変更しない)。処理は新規 `api/meeting_calendar.py` に純関数中心で置く。
   応答:
   ```
   { meeting: {venue, meeting_no, label:"4回中山", first_date, last_known_date, today_day_no, today_week_no},
     weeks: [ {week_no, week_start, course:"A"|"B"|…|null, course_note, rain_mm_week (土日+前2日), days:[...]} ],
     days:  [ {date, weekday, day_no, week_no, course, weather_code, precipitation_mm, precipitation_hours,
               rain_prev_day_mm, turf_condition, dirt_condition, cushion, turf_moisture_goal, dirt_moisture_goal, is_today, source:{...}} ],
     notes: ["使用コースの記録は 2026-09-26 から", ...] }
   ```
   - 開催日の集合 = Neon `races` の同 `回次×場` の日付 ∪ 当日 ∪ (今後の日程は出さない)。`day_no` は kaisai の日目、`week_no` は開催内で「土曜始まりの週」を古い順に 1,2,3… (月曜祝日開催は同じ週)。
   - 降水は Open-Meteo (過去は archive、当日は forecast) を `date×venue` でロギング DB の小テーブル `weather_daily` にキャッシュ (取得日時付き。当日分は 6 時間で再取得)。取得失敗は null。
   - 使用コースは `meeting_course_usage` (週単位) → 無ければアーカイブ PDF の日別値 → 無ければ null (「記録なし」)。
4. **UI** (`index.html` / `script.js` / `style.css`、レース詳細 TOP のコース・風セクションの直下に折りたたみパネル `#meetingCalendar`、既定で開く):
   - 見出し: `4回中山 ・ 開幕3週目 (第6日) ・ 今週 Cコース`。
   - 表 (1 行 = 開催日): `週 | 日 | 日付(曜) | 使用コース | 天候 | 降水 (当日 / 前日) | 芝 | ダート | クッション | 含水率 芝/ダ`。今日の行を強調。週の境目に区切り線。降水 ≥ 1mm を青、≥ 10mm を濃く。使用コースが前週から変わった行に「A→B」バッジ。
   - 天候は weather_code を 晴/曇/雨/雪 などに変換 (WMO コード表を JS に持つ)。
   - 記録なし・取得失敗は「—」と注記。埋め込み (統合版) 以外 (本番 Web) では API が無いのでパネルを表示しない (fetch 404 で非表示)。
5. **本番 Web への影響なし**: `api/index.py` 不変、`script.js` は API 404 時に何もしない。

## 2. テスト (`tests/test_t82_meeting_calendar.py`)
- kaisai 文字列の解析 (回次・日目・場)、週番号の割当 (土日 + 月曜祝日、飛び週)、アーカイブ PDF テキスト (fixture: `data/t50/raw/2026/nakayama03.pdf` の抽出テキストを保存) からの日別 使用コース の解析、馬場情報ページ HTML (fixture、単一場と複数場) からの使用コース抽出、Open-Meteo 応答の整形と weather_code 変換、キャッシュの再取得規則、`meeting_course_usage` の append-only、API の応答形 (Neon をモック)、本番 Web で API が無いときに UI が何も描かないこと (文字列検査)。
- 既存 `tests/test_t73b_autofill.py` `tests/test_jra_suite.py` `tests/test_t76_wind_overlay.py` が通ること。通知関数 3 つの AST 不変テストが通ること。

## 3. 受け入れ (手動・ユーザー)
1. 統合版のレース詳細で中山のレースを開くと、「4回中山・開幕N週目」と日別の表 (使用コースは今週分のみ、それ以前は記録なし) が出る。
2. 次の開催週 (9/26〜) 以降、週ごとの使用コースが自動で積み上がる。
3. 雨の日 (9/20, 9/21) の降水量と馬場 (重/不良) が表に出る。

## 4. やらないこと
- 使用コースから枠バイアスを推定して予測に反映する (表示のみ)。
- 進行中開催の記録開始前の週の使用コースの推定 (「記録なし」のまま。開催終了後にアーカイブ PDF から `--archive` で補完可能)。
- 本番 Web (Vercel) への展開。
