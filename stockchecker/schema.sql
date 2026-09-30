CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    slack_webhook TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    contact_name TEXT,
    email TEXT NOT NULL,
    -- 1 のとき、この仕入先への見積依頼メールは承認なしで自動送信する
    auto_send_rfq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    maker TEXT,
    spec TEXT,
    unit TEXT NOT NULL DEFAULT '個',
    quantity INTEGER NOT NULL DEFAULT 1,
    required_date TEXT,               -- 必要納期 (YYYY-MM-DD)
    budget_unit_price REAL,           -- 予算単価
    custom_item INTEGER NOT NULL DEFAULT 0,  -- 特注品 (常に見積必須)
    owner_id INTEGER REFERENCES staff(id),
    preferred_supplier_id INTEGER REFERENCES suppliers(id),
    notes TEXT,
    watch_urls TEXT,                 -- 価格を監視する商品ページ URL (改行区切り)
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 価格・納期の観測履歴 (相場情報・過去見積・カタログ価格など)
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    source TEXT NOT NULL,             -- manual / csv / http / quote など
    supplier_id INTEGER REFERENCES suppliers(id),
    unit_price REAL,
    currency TEXT NOT NULL DEFAULT 'JPY',
    lead_time_days INTEGER,
    stock_qty INTEGER,
    min_order_qty INTEGER,
    vendor TEXT,                      -- 採用した販売元 (Mouser / モノタロウ 等)
    url TEXT,                         -- 商品ページ
    detail TEXT,                      -- 取得した全オファー (JSON)
    observed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- 判定ルールのコード
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',  -- open / resolved
    notified_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER REFERENCES materials(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id),
    kind TEXT NOT NULL,               -- rfq (見積依頼) / order (注文) / alert
    to_addr TEXT NOT NULL,
    cc_addr TEXT,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', -- draft / sent / failed
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    sent_at TEXT
);

-- サイトごとに利用者が指定した「価格の見出し」
CREATE TABLE IF NOT EXISTS site_hints (
    domain TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tax TEXT                          -- incl (税込) / excl (税抜) / NULL
);

-- アプリ設定 (チームで共有する設定。管理者が画面から変更)
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- メールテンプレート (種別ごと。未登録なら既定の文面を使う)
CREATE TABLE IF NOT EXISTS mail_templates (
    kind TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_by INTEGER REFERENCES staff(id),
    updated_at TEXT
);

-- 1 通のメールに含まれる部材 (まとめて依頼したメールの回答管理用)
CREATE TABLE IF NOT EXISTS email_materials (
    email_id INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    PRIMARY KEY (email_id, material_id)
);

-- 一括取得ジョブ
CREATE TABLE IF NOT EXISTS fetch_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'running',   -- running / cancelling / done / cancelled / failed
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    current TEXT,
    message TEXT,
    started_by INTEGER REFERENCES staff(id),
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_job_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES fetch_jobs(id) ON DELETE CASCADE,
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    status TEXT NOT NULL,                     -- ok / error / skipped
    unit_price REAL,
    lead_time_days INTEGER,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 操作履歴 (誰がいつ何をしたか)
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES staff(id),
    material_id INTEGER REFERENCES materials(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 部材ごとのコメント (チーム内の連絡)
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES staff(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 発注 (1 部材 1 行。注文メールの送信時、または手動で登録)
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id),
    email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    quantity INTEGER NOT NULL,
    unit_price REAL,
    order_date TEXT NOT NULL,          -- 発注日
    required_date TEXT,                -- 希望納期
    promised_date TEXT,                -- 仕入先の回答納期 (入荷予定日)
    received_qty INTEGER NOT NULL DEFAULT 0,
    received_date TEXT,                -- 最後に入荷した日
    status TEXT NOT NULL DEFAULT 'ordered',  -- ordered / confirmed / partial / received / cancelled
    note TEXT,
    created_by INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);

-- 入荷記録 (分納に対応)
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL,
    received_date TEXT NOT NULL,
    note TEXT,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 取り込んだ受信メール (仕入先からの返信)
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    in_reply_to TEXT,
    from_addr TEXT,
    from_name TEXT,
    subject TEXT,
    received_at TEXT,
    body TEXT,
    source TEXT,                       -- imap / upload
    matched_email_id INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    match_method TEXT,
    supplier_id INTEGER REFERENCES suppliers(id),
    extracted TEXT,                    -- 抽出した価格・納期 (JSON)
    status TEXT NOT NULL DEFAULT 'new', -- new / applied / ignored
    handled_by TEXT,
    handled_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Excel 取込のプレビュー (確認後に反映)
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    data TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview',  -- preview / applied
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
