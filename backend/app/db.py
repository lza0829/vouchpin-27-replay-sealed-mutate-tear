from __future__ import annotations
import sqlite3
from app.config import DB_PATH

# 事实状态：open（待钉）/ sealed（已钉，金额不可改不可删）/ void（重放时作废旧 open 留存）
SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  face_value REAL NOT NULL,
  min_spend REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS coupon_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  coupon_code TEXT NOT NULL,
  campaign_code TEXT NOT NULL,
  amount REAL NOT NULL DEFAULT 0,
  business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS redeem_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  coupon_code TEXT NOT NULL,
  campaign_code TEXT NOT NULL,
  amount REAL NOT NULL,
  business_day TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  batch_id INTEGER,
  status TEXT NOT NULL DEFAULT 'open',
  CHECK (status IN ('open', 'sealed', 'void'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_redeem_facts_open_code
  ON redeem_facts(coupon_code) WHERE status='open';
CREATE TABLE IF NOT EXISTS redeem_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  fact_count INTEGER NOT NULL DEFAULT 0,
  total_amount REAL NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  fingerprint TEXT,
  pin_snapshot TEXT
);
'''


def connect(immediate: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # 重建 ∥ 钉扎 并发写时排队等锁而非直接报 locked，由 BEGIN IMMEDIATE 串行化
    conn.execute("PRAGMA busy_timeout=8000")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn


def _migrate_facts_table(conn: sqlite3.Connection) -> None:
    """旧库 redeem_facts.coupon_code 为全表 UNIQUE，重放需要同码并存 void 行。

    重建为部分唯一索引（仅 open 行券码唯一），数据原样搬回。
    判定依据：旧表会带 sqlite_autoindex 自动索引；新表只有显式的部分唯一索引。
    """
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND tbl_name='redeem_facts' AND name LIKE 'sqlite_autoindex%' LIMIT 1"
    ).fetchone()
    if legacy is None:
        return
    conn.executescript(
        '''
        ALTER TABLE redeem_facts RENAME TO redeem_facts_old;
        CREATE TABLE redeem_facts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          coupon_code TEXT NOT NULL,
          campaign_code TEXT NOT NULL,
          amount REAL NOT NULL,
          business_day TEXT NOT NULL,
          event_id INTEGER NOT NULL,
          batch_id INTEGER,
          status TEXT NOT NULL DEFAULT 'open',
          CHECK (status IN ('open', 'sealed', 'void'))
        );
        INSERT INTO redeem_facts
          (id, coupon_code, campaign_code, amount, business_day, event_id, batch_id, status)
          SELECT id, coupon_code, campaign_code, amount, business_day, event_id, batch_id, status
          FROM redeem_facts_old;
        DROP TABLE redeem_facts_old;
        CREATE UNIQUE INDEX ux_redeem_facts_open_code
          ON redeem_facts(coupon_code) WHERE status='open';
        '''
    )


def _migrate_batches_table(conn: sqlite3.Connection) -> None:
    """老库 redeem_batches 无 fingerprint/pin_snapshot 列。

    加列后，按各批当前 sealed 事实回填钉时快照与指纹（老批以现状为钉时基线）。
    此后任何对 sealed 行/批头的静默改写都会在读取时被指纹比对暴露。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(redeem_batches)").fetchall()}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE redeem_batches ADD COLUMN fingerprint TEXT")
    if "pin_snapshot" not in cols:
        conn.execute("ALTER TABLE redeem_batches ADD COLUMN pin_snapshot TEXT")
    rows = conn.execute(
        "SELECT id FROM redeem_batches WHERE status='sealed' "
        "AND (fingerprint IS NULL OR pin_snapshot IS NULL) ORDER BY id"
    ).fetchall()
    if not rows:
        return
    from app.repositories.batches import compute_pin_artifacts, set_pin_artifacts
    for r in rows:
        bid = int(r["id"])
        facts = conn.execute(
            "SELECT id, coupon_code, campaign_code, amount, business_day, event_id "
            "FROM redeem_facts WHERE batch_id=? AND status='sealed' ORDER BY id",
            (bid,),
        ).fetchall()
        fingerprint, snapshot = compute_pin_artifacts([dict(f) for f in facts])
        set_pin_artifacts(conn, bid, fingerprint, snapshot)


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate_facts_table(conn)
        _migrate_batches_table(conn)
        conn.commit()
    finally:
        conn.close()
