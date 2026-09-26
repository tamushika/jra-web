# ノート PC で競馬アプリを動かす手順 (2026-09-26)

デスクトップと同じローカルアプリ (統合スイート = オッズ監視 + WIN5 + 実績ダッシュボード +
レース詳細 + 重賞データ、port 5005) を 2 台目の PC で動かすための手順。

**先に読む注意 (重要)**: 監視・通知・仮想運用を 2 台で同時に走らせてはいけない。理由は §4。
ノート PC は「閲覧・解析専用」にするのが既定の使い方。

---

## 1. git にあるもの / 無いもの

コードとルール・モデル定義は 2 つのリポジトリに入っており、`git clone` で全部そろう。

| リポジトリ | 内容 |
|---|---|
| `github.com/tamushika/jra-tools` (親, branch `master`) | 週次登録 `jra_app.py`、`win5_predictor/`、スクレイパ、`jra-web` をサブモジュールとして参照 |
| `github.com/tamushika/jra-web` (子, branch `main`) | 統合スイート本体・解析ロジック・モデル JSON・ルール CSV・テスト・docs |

git に**入っていない**もの (`.gitignore` 済み) は次の 3 種類。§3 でコピーする。

1. **秘密情報**: `jra-web/.env` (Neon の接続文字列・LINE トークン)
2. **派生成果物 (約 20MB)**: as-of 統計・複勝モデル・血統キャッシュ・重賞キャッシュ
3. **大容量 DB (約 1.2GB)**: `ability.db` / `api/past_data_v2.db` / `data/jra_logging.db`

---

## 2. セットアップ

### 2.1 必要なもの
- Python 3.14 (デスクトップと同じ系列。`python --version` で確認)
- Git
- 任意: Node.js (`node --check script.js` を使うときだけ)

### 2.2 クローン

親リポジトリのルートがそのまま Python の仮想環境になっている構成なので、
**クローンしたディレクトリの中に venv を作る**とデスクトップと同じ形になる。

```powershell
git clone --recurse-submodules https://github.com/tamushika/jra-tools.git C:\jra
cd C:\jra
python -m venv .
.\Scripts\python.exe -m pip install --upgrade pip
.\Scripts\python.exe -m pip install -r requirements.txt
.\Scripts\python.exe -m pip install -r jra-web\requirements.txt
```

`Lib/` `Scripts/` `Include/` `pyvenv.cfg` は .gitignore 済みなので、venv を作っても
作業ツリーは汚れない。クローン先のパスは任意 (日本語・空白を含まないパスが無難)。

サブモジュールを付け忘れた場合は `git submodule update --init --recursive`。

### 2.3 `.env` を用意

```powershell
copy jra-web\.env.example jra-web\.env
```

デスクトップの `jra-web\.env` から値を写す。キーは 3 つ。

| キー | 役割 | ノート PC での推奨 |
|---|---|---|
| `DATABASE_URL` | Neon PostgreSQL。過去データ分析・トラックバイアス・週次登録 | デスクトップと同じ値 (読み取りは同時でも安全) |
| `EV_LINE_CHANNEL_TOKEN` | LINE 通知の送信先 | **空にする** (二重通知の防止。§4) |
| `EV_THRESHOLD` | EV 通知の閾値 | `1.3` |

### 2.4 git に無いファイルを持ち込む

デスクトップ側で zip を作る:

```powershell
cd C:\Users\owner\project\.venv\jra-web
..\Scripts\python.exe -X utf8 make_laptop_bundle.py --list      # 中身の確認
..\Scripts\python.exe -X utf8 make_laptop_bundle.py             # 約20MB の zip を作成
```

`outputs\laptop_bundle_<日付>.zip` ができる。ノート PC のクローン先 `jra-web\` 直下で展開すると
同じ相対パスに戻る。同梱されるのは次の 4 つ。

| ファイル | 大きさ | 無いとどうなるか | 再生成手段 |
|---|---|---|---|
| `api/data_files/common/factor_snapshot.json` | 1.3MB | 採点が古い CSV 統計にフォールバックし、デスクトップと結果がずれる | `retrain_all.bat` (要 ability.db) |
| `web_place_model.json` | 2.4KB | 複勝率β列が出ない | `backtest_place_model.py` |
| `pedigree_cache.json` | 2.8MB | 父・母父の表示と `sire_pts` が欠ける | Neon から再生成 |
| `data/graded_cache.sqlite` | 16MB | 重賞データタブが「キャッシュがありません」になる | `build_graded_cache.py` (要 ability.db・約5秒) |

`.env` も zip に入れたい場合は `--include-env` (秘密情報が zip に入るので取り扱い注意)。

### 2.5 大容量 DB (必要なときだけ)

| ファイル | 大きさ | 必要になる場面 |
|---|---|---|
| `jra-web/ability.db` | 621MB | バックテスト・再学習・重賞キャッシュの再生成。**日常の予想には不要** |
| `jra-web/api/past_data_v2.db` | 304MB | `DATABASE_URL` が使えないときの過去データ分析の代替。Neon があれば不要 |
| `jra-web/data/jra_logging.db` | 269MB | 実績ダッシュボードの履歴。コピーしないとノート PC では空から始まる |

`make_laptop_bundle.py --with-db` で同梱できるが約 1.2GB になるので、
USB か外付けドライブで直接コピーするほうが速い。

### 2.6 起動

```powershell
cd C:\jra\jra-web
start_suite.bat
```

ブラウザで `http://localhost:5005/`。タブは オッズ監視 / WIN5予想 / 実績ダッシュボード /
重賞データ / レース詳細。起動スクリプトは同梱 venv の Python を最優先で使う。

---

## 3. 動作確認

1. `http://localhost:5005/` が開き、左上に「🏇 解析開始」がある。
2. 重賞データタブに今週末の重賞 (例: 9/26 シリウスS・9/27 スプリンターズS) が並ぶ。
3. レース詳細でレースを開くと、コース図に風の矢印と開催カレンダーが出る。
4. テスト: `..\Scripts\python.exe -X utf8 -m pytest tests -q`
   (`ability.db` が無い環境では特徴量系のテストが落ちるので、その場合は
   `-k "not feature_parity and not fold_stats"` などで絞る)

---

## 4. 2 台運用のルール (必ず守る)

同じアプリを 2 台で「監視モード」にすると壊れるものがある。

- **LINE 通知が二重に飛ぶ**。EV 通知・WIN5 確信度通知はどちらの PC からも送られる。
  → ノート PC の `.env` では `EV_LINE_CHANNEL_TOKEN` を空にする。
- **`data/jra_logging.db` が台ごとに分かれる**。オッズスナップショット・予測ログ・
  仮想運用 (T70)・確信度スナップショット (T62b) は各 PC のローカル SQLite に溜まる。
  2 台で監視すると prospective の蓄積が分断され、T63 (通知閾値) / T70 (仮想運用ゲート) /
  T17 (時点別オッズ) の集計が成立しなくなる。
  → **タスクスケジューラ `JRA_Suite_Monitor` はデスクトップにだけ登録する**。ノート PC には登録しない。
- **週次登録 (`jra_app.py` / `batch_register.py`) は片方だけで実行する**。Neon への二重登録を避ける。
- Neon の**読み取り** (過去データ分析・トラックバイアス・重賞の結果表) は 2 台同時で問題ない。

つまり、ノート PC では「解析開始を押して結果を見る」「重賞データを見る」までは自由で、
週末の自動監視と週次登録はデスクトップに任せる。

---

## 5. 更新のしかた

```powershell
cd C:\jra
git pull
git submodule update --init --recursive     # jra-web を親のポインタに合わせる
.\Scripts\python.exe -m pip install -r jra-web\requirements.txt   # 依存が増えたときだけ
```

- コード変更はこれだけで反映される (起動中なら `start_suite.bat` を再起動)。
- `factor_snapshot.json` はデスクトップで定期再学習を回したときに更新されるので、
  そのタイミングで `make_laptop_bundle.py` を作り直して持ち込む。
- `data/graded_cache.sqlite` は重賞が増えた週に更新すると表が新しくなる
  (ability.db があるノート PC なら `build_graded_cache.py` を直接実行してもよい)。

---

## 6. よくあるつまずき

| 症状 | 原因と対処 |
|---|---|
| 起動時に `ModuleNotFoundError` | venv に依存が入っていない。`.\Scripts\python.exe -m pip install -r jra-web\requirements.txt` |
| トラックバイアスが `psycopg2 import error` | `pip install psycopg2-binary`、または `.env` の `DATABASE_URL` を確認 |
| 重賞データが「キャッシュがありません」 | `data/graded_cache.sqlite` が未配置。§2.4 の zip を展開する |
| 過去データ分析が空 | `DATABASE_URL` 未設定かつ `api/past_data_v2.db` が無い。どちらかを用意する |
| ポート 5005 が使用中 | 既に起動している。二重起動はポートガードが止める |
| 採点がデスクトップと微妙に違う | `factor_snapshot.json` が無い。§2.4 で配置する |
