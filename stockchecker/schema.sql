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
