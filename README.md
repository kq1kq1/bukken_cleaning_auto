# 物件のネット掲載金額・販売状況自動変更bot

自社の物件公開CSVと REINS（レインズ）物件DBを照合し、**成約済み・価格変更の物件を検出**して、
2つの自社物件サイト（スカイヤーズ／ピタクラ）を**自動でクリーニング（成約処理・価格変更）**するツールです。

- CSV ⇔ REINS DB のルールベース照合（マンション／戸建／土地）
- 成約候補・価格変更・成約確定をメールで通知
- 確認用GUIで人の目チェック → ボタン1つで2サイトを自動更新（Playwright）
- 自動更新の結果も別メールで通知

---

## 機能概要

| 分類 | 意味 | 動作 |
|---|---|---|
| 一致 | DBにあり価格も同じ | 何もしない |
| 価格変更 | DBにあり価格が違う | 通知 → 確認後に自動で価格更新 |
| 成約確定 | 物件DBになく、成約・取消シートに**直近の記録**あり | 通知 → 確認後に自動で成約処理 |
| 成約候補 | DBにも成約・取消にも無い／記録が古い | 通知のみ（手動確認） |

照合判定（要点）:
- **マンション**: 住所（丁目）＋建物名＋所在階が一致すれば同一住戸
- **戸建・土地**: 住所（丁目）＋最寄駅（駅名＋徒歩/バス分数が完全一致）＋会社名。DBが「一般媒介」のときは会社名でなく土地面積で判定
- **成約確定**: 成約・取消日が「実行日から10日以内」のものだけ対象（古い記録は誤判定防止のため成約候補へ）

---

## 動作環境

- OS: Windows
- Python: 3.13（標準インストーラ。`tkinter` 同梱版）
- 主要ライブラリ: pandas / openpyxl / playwright（`requirements.txt` 参照）

---

## セットアップ（新しいマシンで使うとき）

### 1. リポジトリを取得
```cmd
git clone https://github.com/kq1kq1/bukken_cleaning_auto.git
cd bukken_cleaning_auto
```

### 2. Python 3.13 をインストール
[python.org](https://www.python.org/) の Windows インストーラで導入（`tkinter` を含む標準構成でOK）。

> **重要（別マシンで使う場合）**: `.bat` ファイルは Python の絶対パス（`C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe`）をハードコードしています。ユーザー名が違うマシンではこのパスが存在しないため、**インストール時に「Add Python to PATH」にチェック**を入れて `python` をPATHに通してください（その場合 bat は自動でフォールバックして動きます）。または各 `.bat` の `set "PYTHON=..."` 行を自分の環境のパスに書き換えてください。

### 3. 依存ライブラリをインストール
```cmd
python -m pip install -r requirements.txt
```

### 4. Playwright のブラウザ本体をインストール（重要・忘れやすい）
```cmd
python -m playwright install chromium
```

### 5. config.json を作成
`config.example.json` をコピーして `config.json` を作り、各値を自分の環境に合わせて編集します。
```cmd
copy config.example.json config.json
```
編集が必要な項目:

| 項目 | 内容 |
|---|---|
| `db_path` | reins_db.xlsx のフルパス（後述） |
| `notification.email_from` | 送信元 Gmail アドレス |
| `notification.email_to` | 通知先メール（配列で複数可） |
| `notification.smtp_password` | **Gmailアプリパスワード**（通常のログインPWではない。Googleアカウントで2段階認証→アプリパスワードを発行） |
| `web_update.skyhrs.login_url` / `search_url` | スカイヤーズ（自社サイト）のログイン／物件検索ページURL |
| `web_update.pitat.*` | ピタクラ（https://buy.pitat-cloud.com）のURL。基本そのままでOK |
| `matching.confirmed_sold_within_days` | 成約確定の対象にする日数（既定10日） |

> `config.json` と `auth_state_*.json` は **機密情報のため `.gitignore` で除外**されています（GitHubには上がりません）。

### 6. reins_db.xlsx を用意（別プロジェクト依存）
DB照合の元データ `reins_db.xlsx` は、**別プロジェクト `reins_auto`（半自動クリーニング）が生成**します。
新マシンでは:
1. `reins_auto` 側もセットアップして `reins_db.xlsx` を生成できる状態にする
2. 生成された `reins_db.xlsx` のパスを `config.json` の `db_path` に設定する

`reins_db.xlsx` には2シートが必要です:
- `物件DB`（現在掲載中の物件。列: 建物名 / 所在地 / 価格 / 会社名 / 物件種別 / 沿線駅 / 交通 / 取引態様 / 所在階 / 各面積 など）
- `成約・取消`（成約/取消済み。上記＋`成約・取消日`）

### 7. ログインセッションを保存（初回のみ）
2サイトは ID/パスワードでログインしてセッション（Cookie）を保存し、以降は再利用します（2FAなし）。
```cmd
ログイン設定.bat をダブルクリック
```
→ スカイヤーズのブラウザが開く → 手動ログイン → コンソールで Enter
→ 続けてピタクラのブラウザが開く → 手動ログイン → Enter
→ `auth_state_skyhrs.json` / `auth_state_pitat.json` が保存される

> セッションが切れたら（ログイン画面に飛ばされる）、再度 `ログイン設定.bat` を実行してください。

---

## 使い方（日常運用）

### ① 入力データの準備
自社管理システム（ハウスフリード系）から**物件一覧CSV**をエクスポートします。
- 1行目が見出し行
- 物件管理番号の列（`物件管理番号`、`HF...` 形式）を含むこと
- 文字コードは Shift-JIS / UTF-8 どちらでも自動判定

### ② 照合（CSV vs REINS DB）
`実行.bat` に CSV ファイルを**ドラッグ＆ドロップ**します。
- 照合 → **照合結果メール**を送信
- `reports/` にHTMLレポート保存
- `last_result.json` に結果保存（次の自動更新で使用）

（`実行.bat` をダブルクリックするとファイル選択ダイアログでも選べます）

### ③ 確認して自動更新
`確認して更新.bat` をダブルクリックすると確認GUIが開きます。
1. 成約確定・価格変更が**全部チェック済み**で一覧表示（各物件は **CSV側＝青／DB側＝緑**で並記）
2. 怪しい物件の**チェックを外す**（＝除外）
3. 「ドライラン」で動作確認、または「チェックした物件を更新実行」で本番更新
4. 実行後、**自動更新結果メール**を送信（成功／失敗／スキップ／⚠️要確認）

> 自動更新は **必ず人の目で確認してから**実行する設計です（CSVは手入力のためズレが起こり得るため）。

---

## ファイル構成

| ファイル | 役割 |
|---|---|
| `check_csv.py` | CSV ⇔ REINS DB 照合のメイン。照合結果メール送信、`last_result.json` 出力 |
| `web_updater.py` | スカイヤーズ／ピタクラの成約処理・価格変更を自動実行（Playwright） |
| `review_and_update.py` | 確認＆実行GUI（チェックリスト＋CSV/DB比較表示） |
| `login_setup.py` | 初回ログインしてセッションを保存 |
| `mailer.py` | 照合結果／自動更新結果のHTMLメール生成・送信 |
| `column_map.json` | CSV列名 → 標準フィールドの対応（自動検出できない場合に使用） |
| `config.example.json` | 設定テンプレート（コピーして `config.json` を作る） |
| `実行.bat` | CSV照合（ドラッグ＆ドロップ） |
| `ログイン設定.bat` | 初回ログインセッション保存 |
| `確認して更新.bat` | 確認＆自動更新GUIを起動（**本番更新はこの画面から**） |
| `自動更新_確認.bat` | コマンドからのドライラン（全件、実更新なし） |

### CLIでの自動更新（GUIを使わない場合）
```cmd
python web_updater.py                         :: ドライラン（既定。実更新なし）
python web_updater.py --execute               :: 本番実行
python web_updater.py --only HF000000          :: 特定の物件管理番号だけ（カンマ区切り可）
python web_updater.py --limit 5                :: 先頭5件だけ
python web_updater.py --site skyhrs            :: スカイヤーズだけ（既定: both）
python web_updater.py --show                   :: ブラウザを表示（デバッグ）
```

---

## 列マッピング（column_map.json）

CSVの列名が自動検出できない場合、`column_map.json` の右辺をCSVの実際の列名に書き換えます。
現在はハウスフリード系CSVに合わせて設定済み。`_` で始まる列は `preprocess_csv()` が自動生成する派生列です（編集不要）。

---

## 注意・既知の制約

- **CSVは手入力**のため、建物名・住所・最寄駅・会社名にゆらぎが出ます。照合は表記ゆれ（漢数字・記号・カタカナ↔英字・法人格・営業所名など）を吸収していますが、最終的にGUIの目視確認で担保します。
- **reins_db.xlsx の精度に依存**します。`reins_auto` が誤って成約確定にすると、本ツールが「実際は売れていない物件」を成約処理してしまうリスクがあります。→ GUIでの確認が最後の防波堤です。
- スカイヤーズの検索で**複数件ヒット**した場合は、表示された全件をまとめて更新します（同一物件の重複掲載対策）。結果メールの「⚠️要確認」に明記されます。
- 既に目標状態（既に成約／既に新価格）の物件は更新せずスキップします（再実行しても安全）。
- **処理時間の目安**: 自動更新は1件ずつ直列で処理します。特にピタクラはページのロードが遅く、1件あたり数秒〜十数秒かかります。**対象が100件を超える日は、2サイト合わせて30〜40分程度**かかることがあります（途中でブラウザを閉じないでください）。件数が多い日は時間に余裕を持って実行してください。
- `reports/` のHTMLレポートは **14日より古いものを自動削除**します（`check_csv.py` の `REPORT_KEEP_DAYS` で変更可）。
- **印刷用ページ（分担用）**: 照合実行時に `reports/print_review_*.html` を自動生成します。要確認物件（成約候補）を **1ページ10件区切り**で並べたA4縦のHTMLで、ブラウザで開いて `Ctrl+P` で印刷すると改ページされます。件数を変えたい場合は `config.json` の `matching.review_print_per_page` を編集してください（既定10件）。

---

## トラブルシュート

| 症状 | 対処 |
|---|---|
| `セッション切れ` と出る | `ログイン設定.bat` で再ログイン |
| `playwright` でブラウザ起動失敗 | `python -m playwright install chromium` を実行したか確認 |
| CSVの列が検出できない | `column_map.json` を実際の列名に合わせて編集 |
| 自動更新で詳細ページが出ない等 | `debug_*.html` / `debug_*.png` が保存されるので確認。ログは `web_updater.log` |
| 照合結果がおかしい | `checker.log` を確認。`reports/` のHTMLで内訳確認 |

ログファイル: `checker.log`（照合）/ `web_updater.log`（自動更新）

---

## 関連プロジェクト

- **reins_auto**: REINSから物件DB（`reins_db.xlsx`）を生成する半自動クリーニング。本ツールの入力元。
