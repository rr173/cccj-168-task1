"""SQLite 持久层。单连接 + 服务层可重入锁,所有写操作在 BEGIN IMMEDIATE
事务中串行化,保证并发投票/迟到事件不会撕裂快照。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  phase TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  quorum INTEGER NOT NULL,            -- 决策所需最低投票数
  approve_threshold INTEGER NOT NULL, -- 通过所需最低赞成票数
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS committee_members(
  trial_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(trial_id, member_id)
);
CREATE TABLE IF NOT EXISTS rulesets(
  id TEXT PRIMARY KEY,
  trial_id TEXT NOT NULL,
  version TEXT NOT NULL,
  body TEXT NOT NULL,                 -- JSON:规则引擎参数
  created_at TEXT NOT NULL,
  UNIQUE(trial_id, version)
);
CREATE TABLE IF NOT EXISTS cohorts(
  id TEXT PRIMARY KEY,
  trial_id TEXT NOT NULL,
  label TEXT NOT NULL,
  dose_mg REAL NOT NULL,              -- 冻结:给药方案
  schedule TEXT NOT NULL,             -- 冻结:给药计划
  window_days INTEGER NOT NULL,       -- 冻结:观察窗天数
  expanded INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  state_version INTEGER NOT NULL DEFAULT 1,  -- 单调递增:任何影响决策的变更 +1
  frozen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subjects(
  id TEXT PRIMARY KEY,
  cohort_id TEXT NOT NULL,
  code TEXT NOT NULL,
  status TEXT NOT NULL,
  data_status TEXT NOT NULL DEFAULT 'PENDING',
  replacement_of TEXT,
  replaced_by TEXT,
  enrolled_at TEXT NOT NULL,
  UNIQUE(cohort_id, code)
);
CREATE TABLE IF NOT EXISTS doses(     -- 已给药事实:任何情况下不可修改/删除
  id TEXT PRIMARY KEY,
  cohort_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  dose_mg REAL NOT NULL,
  given_at TEXT NOT NULL,             -- 给药日期 YYYY-MM-DD
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY,
  cohort_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  description TEXT NOT NULL,
  severity INTEGER NOT NULL,          -- CTCAE 1-5
  evaluability TEXT NOT NULL DEFAULT 'PENDING',
  related INTEGER NOT NULL DEFAULT 1,
  late INTEGER NOT NULL DEFAULT 0,    -- 迟到事件:报告时该受试者观察窗已结束
  occurred_at TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(  -- 决策会话:钉住单一快照
  id TEXT PRIMARY KEY,
  cohort_id TEXT NOT NULL,
  intent TEXT NOT NULL,
  status TEXT NOT NULL,
  state_version INTEGER NOT NULL,     -- 开会时的队列状态版本
  rule_version TEXT NOT NULL,         -- 开会时的规则版本
  snapshot_hash TEXT NOT NULL,        -- 开会时的快照哈希
  quorum INTEGER NOT NULL,
  approve_threshold INTEGER NOT NULL,
  outcome TEXT,
  snapshot_json TEXT NOT NULL DEFAULT '',   -- 钉住的完整快照(证明的数据源)
  evaluation_json TEXT NOT NULL DEFAULT '', -- 开会时的规则评估
  opened_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS votes(
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  vote TEXT NOT NULL,
  state_version_seen INTEGER NOT NULL, -- 投票人投票时看到的快照版本
  cast_at TEXT NOT NULL,
  UNIQUE(session_id, member_id)        -- 每人每会话一票
);
CREATE TABLE IF NOT EXISTS pauses(
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,                -- COHORT / TRIAL
  scope_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  paused_at TEXT NOT NULL,
  resumed_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger(    -- 决策证明账本:哈希链,只追加
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 显式管理事务(BEGIN IMMEDIATE / COMMIT)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
