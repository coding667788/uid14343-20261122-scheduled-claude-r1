"""SQLite 持久化：plan 命令可把计划存入数据库，check 命令可取回校验。"""
from __future__ import annotations

import hashlib
import json
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def case_hash(case_obj):
    """同一逻辑输入得到同一哈希：键排序后的紧凑 JSON 取 SHA-256。"""
    canonical = json.dumps(case_obj, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    return conn


def save_plan(db_path, case_obj, plan):
    """把计划追加存入数据库，返回存储行 id。"""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO plans (case_hash, plan_json) VALUES (?, ?)",
            (case_hash(case_obj), json.dumps(plan, ensure_ascii=False,
                                             sort_keys=True)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_latest_plan(db_path, case_obj):
    """取回该用例最近一次存入的计划；没有则返回 None。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT plan_json FROM plans WHERE case_hash = ? "
            "ORDER BY id DESC LIMIT 1",
            (case_hash(case_obj),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return json.loads(row[0])
