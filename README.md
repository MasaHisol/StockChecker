# StockChecker — 部材価格・納期トラッカー

登録した部材の **一般価格・納期** を定期的に追跡し、見積取得が必要な部材を自動判定して
**担当者へアラート**、**見積依頼・注文メールの下書き作成／送信 (自動送信も可)** を行う Web アプリです。

## 機能

| 機能 | 内容 |
|---|---|
| 部材登録 | 品番・品名・メーカー・仕様・数量・必要納期・予算単価・特注品フラグ・担当者・主仕入先 |
| 価格・納期の追跡 | 手入力 / 価格表 CSV 取込 / HTTP 価格フィード (API) / デモ相場。履歴と単価推移グラフを表示 |
| 見積必須判定 | 下表のルールで自動判定 (しきい値は環境変数で変更可) |
| 担当者アラート | 新規に発生した判定をまとめて担当者へメール、Slack/Teams Webhook にも通知。解消すると自動クローズ |
| 見積依頼メール | 要見積の部材は主仕入先宛の見積依頼を自動で下書き (担当者 CC)。画面で編集して送信 |
| 自動送信 | `SC_AUTO_SEND=1` かつ仕入先の「自動送信 ON」の場合、承認なしで見積依頼を送信 |
| 注文メール | 部材画面から数量・単価・納期を指定して注文メールを作成・送信 |

### 判定ルール

| コード | 条件 | 区分 |
|---|---|---|
| `no_price` | 価格情報がない | 要見積 |
| `custom_item` | 特注品 | 要見積 |
| `stale_price` | 価格情報が `SC_STALE_DAYS` (30) 日より古い | 要見積 |
| `price_change` | 前回比 ±`SC_PRICE_CHANGE_PCT` (10)% 以上の変動 | 要見積 |
| `over_budget` | 一般単価 > 予算単価 | 要見積 |
| `high_value` | 単価×数量 ≥ `SC_HIGH_VALUE_AMOUNT` (100,000) 円 | 要見積 |
| `lead_time_over` | 今日+一般納期 > 必要納期 | 要見積 |
| `stock_short` | 流通在庫 < 必要数 | 要見積 |
| `lead_time_tight` | 必要納期までの余裕 < `SC_LEAD_MARGIN_DAYS` (3) 日 | 注意 |

同じ部材・仕入先への見積依頼は、下書きが残っている間および送信後 `SC_STALE_DAYS` 日間は重複作成しません。

## ネットでの価格・納期の自動追跡

設定した取得元から、チェックのたびに一般価格・納期・在庫を自動取得します
(exe 版は `stockchecker.ini` の `[sources]`、Python 版は環境変数)。**AI なし・無料の取得元だけでも運用できます。**

| 取得元 | 費用 | 対象 | 設定 |
|---|---|---|---|
| 商品ページ監視 | 無料・キー不要 | モノタロウ・ミスミ等、商品ページが決まっている部材 | 部材の編集画面で URL を登録 (`SC_PAGE_WATCH=1` 既定) |
| Yahoo!ショッピング | 無料 | 汎用品・工具・消耗品 (品番で検索、税込→税抜換算) | `SC_YAHOO_APP_ID` |
| 楽天市場 | 無料 | 同上 | `SC_RAKUTEN_APP_ID` |
| Mouser / Digi-Key | 無料 | 電子部品 (品番完全一致・数量割引・メーカー納期) | `SC_MOUSER_API_KEY` / `SC_DIGIKEY_CLIENT_ID`,`_SECRET` |
| Web 検索 (AI) | 従量課金・任意 | 何でも。他で見つからない部材の最後の手段 | `ANTHROPIC_API_KEY` + `SC_WEB_SEARCH=fallback` |

- **商品ページ監視の使い方**: 部材画面の「商品ページの URL を貼り付けて価格を追跡」に URL を貼るだけ。
  部材の新規登録時も URL から品名・品番・メーカー・価格を自動入力できます。読み取りは次の順に試します:
  1. ページに埋め込まれた商品データ (schema.org JSON-LD / microdata / OGP)
  2. ページ内データの price 項目
  3. サイトごとに記憶した「価格の見出し」
  4. 「販売価格」「単価」などの見出しの近くの金額 (送料・ポイント等は除外、税込は税抜に換算)
  - 静的な HTML で見つからなければ、PC の **Edge / Chrome を裏で起動して JavaScript 実行後のページ**を読み直します
  - それでも判定できない場合は、ページ内の金額一覧から **どれが価格かを 1 回選ぶ**だけ。見出しをサイトごとに記憶し、
    同じサイトの他の商品も以後は自動で読めます
  - ログインしないと価格が見えないページ (会員価格など) は読めません。その場合は価格を手入力して URL だけ登録できます
- 複数の販売元を比較し、**必要数の在庫がある中で最安**を代表値として記録 (比較した全件とリンクも保存)
- 部材画面の「ネットで最新価格を取得」で即時取得も可能

> 取得した価格は参考値です。発注前に商品ページか正式見積で確認してください。
> 各サイトの利用規約に従い、巡回は定期チェック時 (既定 24 時間ごと) のみに留めています。

## exe で使う (Windows・Python 不要)

1. `StockChecker.exe` を入手する
   - GitHub の **Actions → Build Windows exe** の最新実行の「Artifacts」からダウンロード
     (`v1.0` のようなタグを push すると Releases にも添付されます)
   - または Windows 上で `build_exe.bat` をダブルクリックして `dist\StockChecker.exe` を生成
2. 好きなフォルダ (例: `C:\StockChecker`) に置いてダブルクリック → ブラウザが自動で開きます
3. 初回起動時に同じフォルダへ `stockchecker.ini` (設定) と `stockchecker.db` (データ) が作られます。
   SMTP・会社名・自動送信・判定しきい値は `stockchecker.ini` を編集して再起動してください
4. 起動中は `check_interval_hours` (既定 24 時間) ごとに定期チェックを自動実行します。
   終了は黒いウィンドウを閉じるだけです

> 初回は Windows SmartScreen の警告が出ることがあります (「詳細情報」→「実行」)。
> データを残すにはフォルダごとバックアップしてください。

## 使い方 (Python から)

```bash
pip install -r requirements.txt
python -m stockchecker seed            # デモデータ投入 (任意)
python -m stockchecker serve           # http://127.0.0.1:5000
```

定期チェック (価格取得 → 判定 → アラート → 見積依頼) は cron 等で実行します:

```cron
0 8 * * 1-5  cd /path/to/StockChecker && python -m stockchecker check
```

`--demo` を付けるとデモ用の擬似相場で価格を取得します。画面の「今すぐチェック実行」でも同じ処理が走ります。

## 設定 (環境変数)

| 変数 | 既定値 | 説明 |
|---|---|---|
| `SC_DATABASE` | `stockchecker.db` | SQLite ファイル |
| `SC_COMPANY_NAME` / `SC_PURCHASER_NAME` / `SC_PURCHASER_EMAIL` | サンプル値 | メール署名・差出人 |
| `SC_SMTP_HOST` / `SC_SMTP_PORT` / `SC_SMTP_USER` / `SC_SMTP_PASSWORD` / `SC_SMTP_STARTTLS` | 未設定 / 587 / - / - / 1 | SMTP。**未設定の場合は送信せず `SC_OUTBOX_DIR` に .eml を保存** |
| `SC_OUTBOX_DIR` | `outbox` | .eml 保存先 |
| `SC_AUTO_SEND` | `0` | 見積依頼の自動送信を全体で許可 |
| `SC_PRICE_FEED_URL` | 未設定 | HTTP 価格フィード。`GET {url}?part_number=..&maker=..` が `{"unit_price":..,"lead_time_days":..,"stock_qty":..}` (またはその配列) を返す |
| `SC_STALE_DAYS` 他 | 上表参照 | 判定しきい値 |

## 価格表 CSV

```csv
part_number,unit_price,lead_time_days,stock_qty,min_order_qty,currency,observed_at
STM32F407VGT6,1520,60,800,1,JPY,2026-09-29
```

`part_number` 以外は任意。未登録の品番はスキップして画面に表示します。

## 拡張

商社・通販サイトの API に対応するには `stockchecker/providers.py` の `PriceProvider` を継承して
`fetch(material)` を実装し、`app.build_providers` に追加してください。

## 構成

```
stockchecker/
  rules.py      見積必須判定 (純粋関数)
  service.py    定期チェック・アラート・メール作成/送信
  providers.py  価格取得元 (HTTP / デモ / CSV)
  mailer.py     メール文面テンプレートと SMTP/.eml 送信
  app.py        Flask Web UI
  __main__.py   CLI (serve / check / seed)
tests/          pytest
```

テスト: `python -m pytest`
