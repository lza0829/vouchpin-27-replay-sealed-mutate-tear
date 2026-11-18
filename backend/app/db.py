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
  note TEXT NOT NULL DEFAULT ''
);
'''


def connect(immediate: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _migrate_facts_table(conn)
        conn.commit()
    finally:
        conn.close()
