CREATE TABLE IF NOT EXISTS canonical_products (
    canonical_id TEXT PRIMARY KEY,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    ref TEXT,
    color TEXT,
    gender TEXT,
    category TEXT,
    variant TEXT,
    image_url TEXT,
    aliases TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS matched_rows_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_row_id INTEGER,
    canonical_id TEXT,
    confidence TEXT,
    score REAL,
    price_eur REAL,
    source TEXT,
    url TEXT,
    matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (canonical_id) REFERENCES canonical_products(canonical_id)
);

CREATE INDEX IF NOT EXISTS idx_matched_canonical ON matched_rows_v2(canonical_id);
CREATE INDEX IF NOT EXISTS idx_matched_confidence ON matched_rows_v2(confidence);
