"""漏斗数据层：每条视频的全漏斗表现，接回当初生成它的脚本与钩子。

两张表：
  videos  一条视频一行：标题、选题类型、钩子句式、钩子文本、脚本全文、状态。
  metrics 一条观测一行：播放、本地占比、POI 点击、团购订单、核销、到店、办卡、成本。
验收只看这里的数，不看脚本分数。没有真实数据时表是空的，不写估计值进去。
指标缺失一律记 NULL，绝不写 0 —— “不知道”不是“零播放”（GPT 第二次复核
第 5 节第 4 项）；重复记视频用 UPSERT 只更新字段，不删行重建，metrics
外键指向的行不会因此丢掉。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  topic_kind TEXT,
  hook_pattern TEXT,
  hook_text TEXT,
  script_json TEXT,
  created_at TEXT,
  status TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT REFERENCES videos(id),
  observed_at TEXT,
  plays INTEGER,
  local_share REAL,
  poi_clicks INTEGER,
  orders INTEGER,
  redemptions INTEGER,
  visits INTEGER,
  signups INTEGER,
  cost_cents INTEGER,
  source TEXT
);
"""


def _fk_on(conn: sqlite3.Connection) -> None:
    """外键约束按连接生效，每个入口都打开一次（连接池/新连接也不会漏）。"""
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _fk_on(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def record_video(conn: sqlite3.Connection, video: dict) -> None:
    _fk_on(conn)
    # UPSERT：同 id 只更新字段，不删行重建 —— INSERT OR REPLACE 会删掉旧行，
    # metrics 里指向它的外键行会跟着失效（GPT 第二次复核第 5 节第 4 项）。
    conn.execute(
        "INSERT INTO videos"
        " (id, title, topic_kind, hook_pattern, hook_text, script_json, created_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET"
        " title=excluded.title, topic_kind=excluded.topic_kind,"
        " hook_pattern=excluded.hook_pattern, hook_text=excluded.hook_text,"
        " script_json=excluded.script_json, created_at=excluded.created_at,"
        " status=excluded.status",
        (video["id"], video["title"], video.get("topic_kind", ""),
         video.get("hook_pattern", ""), video.get("hook_text", ""),
         video.get("script_json", ""), video.get("created_at", ""),
         video.get("status", "draft")))
    conn.commit()


def record_metrics(conn: sqlite3.Connection, video_id: str, m: dict) -> None:
    _fk_on(conn)
    # 未给的指标记 NULL，不写 0：没观测到 ≠ 观测到 0。
    conn.execute(
        "INSERT INTO metrics"
        " (video_id, observed_at, plays, local_share, poi_clicks, orders,"
        "  redemptions, visits, signups, cost_cents, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video_id, m.get("observed_at", ""), m.get("plays"),
         m.get("local_share"), m.get("poi_clicks"), m.get("orders"),
         m.get("redemptions"), m.get("visits"), m.get("signups"),
         m.get("cost_cents"), m.get("source", "")))
    conn.commit()


def get_funnel(conn: sqlite3.Connection, video_id: str) -> dict | None:
    row = conn.execute(
        "SELECT v.*, m.plays, m.local_share, m.poi_clicks, m.orders,"
        " m.redemptions, m.visits, m.signups, m.cost_cents, m.source, m.observed_at"
        " FROM videos v LEFT JOIN metrics m ON m.video_id = v.id"
        " WHERE v.id = ? ORDER BY m.id DESC LIMIT 1", (video_id,)).fetchone()
    return dict(row) if row else None
