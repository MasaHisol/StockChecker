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

キーを設定した取得元から、チェックのたびに自動で一般価格・納期・在庫を取得します
(exe 版は `stockchecker.ini` の `[sources]`、Python 版は環境変数)。

| 取得元 | 対象 | 必要なもの | 費用 |
|---|---|---|---|
| Mouser API | 電子部品 (品番完全一致) | API キー (`SC_MOUSER_API_KEY`) — mouser.jp の API Hub で無料発行 | 無料 |
| Digi-Key API | 電子部品 (品番完全一致) | Client ID / Secret (`SC_DIGIKEY_CLIENT_ID` / `_SECRET`) — developer.digikey.com | 無料 |
| Web 検索 (Claude) | **何でも** (機構部品・鋼材・工具・汎用品など)。モノタロウ・ミスミ・商社サイト等を検索し、品番/仕様一致の価格・納期を抽出 | Anthropic API キー (`ANTHROPIC_API_KEY`) | 従量課金 (1 部材 1 回あたり数円〜数十円程度) |

- 複数の販売元の結果を比較し、**必要数の在庫がある中で最安**を代表値として記録 (比較した全件と商品ページへのリンクも保存)
- 数量割引は必要数量に適用される単価で計算。在庫が足りればすぐ入荷、不足ならメーカー納期で判定
- Web 検索は費用を抑えるため、既定では **API で見つからなかった部材だけ** (`SC_WEB_SEARCH=fallback`)、
  同じ部材は `SC_WEB_SEARCH_INTERVAL_DAYS` (7) 日に 1 回まで。`always` で全部材、`off` で無効
- 部材画面の「ネットで最新価格を取得」で即時取得も可能
- 取得した価格・納期は前述の判定ルールにかかり、要見積ならアラート・見積依頼メールまで自動で流れます

> Web 検索の結果は AI が公開情報から抽出した参考値です。発注前に必ず商品ページ (リンク) か正式見積で確認してください。

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
