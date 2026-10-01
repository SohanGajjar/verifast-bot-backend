"""SQLite storage for bot-backend: app.db (override the path with APP_DB_PATH).

Tables:
    chat_history    every customer / agent / ai message, one row per message
                      reply_kind  outbound only: 'dm' | 'comment_reply' | 'private_reply'
    webhook_events  raw POST /events bodies from the gateway, one row per delivery
                    (status: stored | duplicate | ignored)
    reply_configs   auto-reply configuration, per index_name (client)
                      type           'dm' | 'comment'
                      received       incoming text to match;  NULL = default (any message)
                      reply          dm rule:      the DM sent back; NULL = default reply
                                     comment rule: the private reply (DM) to the commenter;
                                                   NULL = no DM (public reply only)
                      comment_reply  comment rules only: the public reply under the
                                     comment; NULL = default rule's comment_reply
    auto_replies    every auto-reply attempt, one row per (kind, source_key)
                      mode        'dm' | 'comment_only' | 'comment_and_dm' (comment
                                  rules are comment_and_dm when they have a reply)
                      kind        'dm' | 'comment_reply' | 'private_reply'
                      source_key  id of the incoming DM / comment; the unique index
                                  is the dedup lock for duplicate webhook deliveries
                      status      'pending' -> 'sent' | 'failed' | 'skipped'
"""
import os
import sqlite3

DB_PATH = os.getenv("APP_DB_PATH", os.path.join(os.path.dirname(__file__), "app.db"))

REPLY_TYPES = ("dm", "comment")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE,
    index_name TEXT, session_id TEXT, actor TEXT, text TEXT,
    event_type TEXT, username TEXT, created_at TEXT,
    account_id TEXT, conversation_id TEXT, comment_id TEXT, post_id TEXT,
    reply_kind TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_history_session
    ON chat_history (index_name, session_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    index_name TEXT, session_id TEXT, event_type TEXT,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_webhook_events_request_id
    ON webhook_events (request_id);

CREATE TABLE IF NOT EXISTS reply_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('dm', 'comment')),
    received TEXT,
    reply TEXT,
    comment_reply TEXT CHECK (comment_reply IS NULL OR type = 'comment'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
-- One rule per (index_name, type, received) — received compared trimmed and
-- case-insensitively, as auto_reply matches it; the NULL "default" rule counts too.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reply_configs_rule
    ON reply_configs (index_name, type, LOWER(TRIM(IFNULL(received, ''))));

CREATE TABLE IF NOT EXISTS auto_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name TEXT, session_id TEXT,
    mode TEXT NOT NULL, kind TEXT NOT NULL, source_key TEXT NOT NULL,
    config_id INTEGER, text TEXT,
    reply_id TEXT, parent_comment_id TEXT, post_id TEXT, conversation_id TEXT,
    account_id TEXT,
    status TEXT NOT NULL, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_replies_source
    ON auto_replies (kind, source_key);
CREATE INDEX IF NOT EXISTS idx_auto_replies_session
    ON auto_replies (index_name, session_id);
"""


def _migrate_reply_configs(conn: sqlite3.Connection) -> None:
    """Rebuild a reply_configs table from before `comment_reply`.

    A comment rule's old reply was its public reply, so it moves to comment_reply
    and reply becomes NULL — the rule keeps replying publicly, with no DM.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(reply_configs)")]
    if not columns or "comment_reply" in columns:
        return
    conn.executescript("""
        DROP INDEX IF EXISTS idx_reply_configs_rule;
        ALTER TABLE reply_configs RENAME TO reply_configs_legacy;
    """)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO reply_configs (id, index_name, type, received, reply, comment_reply, "
        "created_at, updated_at) SELECT id, index_name, type, received, "
        "CASE type WHEN 'dm' THEN reply END, CASE type WHEN 'comment' THEN reply END, "
        "created_at, updated_at FROM reply_configs_legacy")
    conn.execute("DROP TABLE reply_configs_legacy")


def _migrate_chat_history_reply_kind(conn: sqlite3.Connection) -> None:
    """Add reply_kind to existing chat_history tables (dm | comment_reply | private_reply).

    Also backfill outbound AI rows from auto_replies when text+session match,
    so older comment_and_dm threads show Comment reply vs Private reply.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(chat_history)")]
    if not columns:
        return
    if "reply_kind" not in columns:
        conn.execute("ALTER TABLE chat_history ADD COLUMN reply_kind TEXT")
    # auto_replies may not exist yet on a brand-new empty connect before SCHEMA runs;
    # callers run SCHEMA first, then this migration.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "auto_replies" not in tables:
        return
    conn.execute(
        "UPDATE chat_history SET reply_kind = ("
        "  SELECT ar.kind FROM auto_replies ar"
        "  WHERE ar.status = 'sent'"
        "    AND ar.index_name = chat_history.index_name"
        "    AND ar.session_id = chat_history.session_id"
        "    AND IFNULL(ar.text, '') = IFNULL(chat_history.text, '')"
        "  ORDER BY ar.id DESC LIMIT 1"
        ") WHERE actor = 'ai' AND (reply_kind IS NULL OR reply_kind = '')"
        "  AND EXISTS ("
        "    SELECT 1 FROM auto_replies ar"
        "    WHERE ar.status = 'sent'"
        "      AND ar.index_name = chat_history.index_name"
        "      AND ar.session_id = chat_history.session_id"
        "      AND IFNULL(ar.text, '') = IFNULL(chat_history.text, '')"
        "  )"
    )


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _migrate_reply_configs(conn)
    conn.executescript(SCHEMA)
    _migrate_chat_history_reply_kind(conn)
    conn.commit()
    return conn
