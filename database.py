"""SQLite storage for bot-backend: app.db (override the path with APP_DB_PATH).

Tables:
    chat_history    every customer / agent / ai message, one row per message
                      reply_kind  outbound only: 'dm' | 'comment_reply' | 'private_reply'
                      reply_id    outbound only: Instagram id of what was sent (the
                                  public reply's comment id / the DM's message id)
                      deleted_at  set once the comment was deleted from Instagram
                      replaces_id an edited comment reply: the chat_history row it replaced
                      message_id  customer DMs: Instagram's id for the message — what
                                  ties a message.edited event to the row it changes
                      edited_at   customer DMs: when the customer last edited it (text
                                  is always the latest version)
                    One customer row per message: unique per index_name on message_id
                    (DMs) and on comment_id (comments), so a duplicate delivery is not
                    stored again.
    webhook_events  raw POST /events bodies from the gateway, one row per delivery
                    (status: stored | duplicate | ignored | edited | stale)
                      zernio_event_id  Zernio's id for the webhook event (empty for
                                       polled messages); a retry keeps it, a second
                                       event for the same message does not
    message_edits   one row per customer edit of a DM (message.edited), oldest first
                      message_id     Instagram's id for the DM (chat_history.message_id)
                      history_id     the chat_history row the edit was applied to
                      previous_text  the text before this edit
                      text           the text after it
                      edited_at      when the customer made it (Zernio's editedAt)
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
    reply_edits     "edit" of a published comment reply (Instagram cannot edit a
                    comment, so an edit posts a replacement and deletes the original)
                      original_id     chat_history row being replaced (UNIQUE: one
                                      edit per reply — re-editing targets the replacement)
                      replacement_id  chat_history row of the new reply
                      status          'pending' -> 'done' | 'partial' (replacement
                                      posted, original still live) | 'failed' (nothing
                                      posted; may be retried)
    session_holds   one row per session an admin has taken over
                      holder_email  the admin who currently holds it; a later takeover
                                    overwrites this (the previous holder is released)
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
    reply_kind TEXT, reply_id TEXT, deleted_at TEXT, replaces_id INTEGER,
    message_id TEXT, edited_at TEXT);
CREATE INDEX IF NOT EXISTS idx_chat_history_session
    ON chat_history (index_name, session_id);

CREATE TABLE IF NOT EXISTS message_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name TEXT, session_id TEXT,
    message_id TEXT NOT NULL,
    history_id INTEGER NOT NULL,
    previous_text TEXT, text TEXT,
    edited_at TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_message_edits_message
    ON message_edits (index_name, message_id);

CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    index_name TEXT, session_id TEXT, event_type TEXT,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    zernio_event_id TEXT);
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

CREATE TABLE IF NOT EXISTS reply_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_name TEXT, session_id TEXT,
    original_id INTEGER NOT NULL UNIQUE,
    replacement_id INTEGER,
    text TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS session_holds (
    index_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    holder_email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (index_name, session_id));
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


def _migrate_chat_history_reply_actions(conn: sqlite3.Connection) -> None:
    """Add reply_id / deleted_at / replaces_id to existing chat_history tables.

    Backfills reply_id on auto-reply rows from auto_replies (same session, kind,
    parent comment and text) so replies sent before this column can be deleted.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(chat_history)")]
    for name, sql_type in (("reply_id", "TEXT"), ("deleted_at", "TEXT"),
                           ("replaces_id", "INTEGER")):
        if name not in columns:
            conn.execute(f"ALTER TABLE chat_history ADD COLUMN {name} {sql_type}")
    match = (
        "  FROM auto_replies ar WHERE ar.status = 'sent' AND ar.reply_id IS NOT NULL"
        "    AND ar.index_name = chat_history.index_name"
        "    AND ar.session_id = chat_history.session_id"
        "    AND ar.kind = chat_history.reply_kind"
        "    AND IFNULL(ar.parent_comment_id, '') = IFNULL(chat_history.comment_id, '')"
        "    AND IFNULL(ar.text, '') = IFNULL(chat_history.text, '')"
    )
    conn.execute(
        f"UPDATE chat_history SET reply_id = (SELECT ar.reply_id {match} ORDER BY ar.id LIMIT 1) "
        f"WHERE actor = 'ai' AND reply_id IS NULL AND EXISTS (SELECT 1 {match})"
    )


def _migrate_chat_history_message_edits(conn: sqlite3.Connection) -> None:
    """Add message_id / edited_at to existing chat_history tables.

    Rows stored before message_id have none, so an edit of one is matched by its
    previous text instead (see message_edits.py) and backfilled then.
    """
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(chat_history)")]
    for name in ("message_id", "edited_at"):
        if name not in columns:
            conn.execute(f"ALTER TABLE chat_history ADD COLUMN {name} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_message "
                 "ON chat_history (index_name, message_id)")


# The customer rows that must be unique per message: (index name, key column, row filter).
CUSTOMER_MESSAGE_KEYS = (
    ("idx_chat_history_customer_dm", "message_id",
     "actor = 'customer' AND IFNULL(message_id, '') != ''"),
    ("idx_chat_history_customer_comment", "comment_id",
     "actor = 'customer' AND event_type = 'comment' AND IFNULL(comment_id, '') != ''"),
)


def _migrate_chat_history_one_row_per_message(conn: sqlite3.Connection) -> None:
    """Make chat_history hold one customer row per message.

    Duplicate deliveries stored before the unique indexes are folded into the first
    row: edits recorded against a later copy move to it, then the copies are removed.
    """
    for index, key, where in CUSTOMER_MESSAGE_KEYS:
        groups = conn.execute(
            f"SELECT index_name, {key}, MIN(id) FROM chat_history WHERE {where} "
            f"GROUP BY index_name, {key} HAVING COUNT(*) > 1").fetchall()
        for index_name, value, keep in groups:
            copies = [r[0] for r in conn.execute(
                f"SELECT id FROM chat_history WHERE {where} AND index_name IS ? "
                f"AND {key} = ? AND id != ?", (index_name, value, keep))]
            marks = ", ".join("?" * len(copies))
            conn.execute(f"UPDATE message_edits SET history_id = ? WHERE history_id IN ({marks})",
                         (keep, *copies))
            conn.execute(f"DELETE FROM chat_history WHERE id IN ({marks})", copies)
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index} "
                     f"ON chat_history (index_name, {key}) WHERE {where}")


def _migrate_webhook_events_zernio_event_id(conn: sqlite3.Connection) -> None:
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(webhook_events)")]
    if "zernio_event_id" not in columns:
        conn.execute("ALTER TABLE webhook_events ADD COLUMN zernio_event_id TEXT")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _migrate_reply_configs(conn)
    conn.executescript(SCHEMA)
    _migrate_chat_history_reply_kind(conn)
    _migrate_chat_history_reply_actions(conn)
    _migrate_chat_history_message_edits(conn)
    _migrate_chat_history_one_row_per_message(conn)
    _migrate_webhook_events_zernio_event_id(conn)
    conn.commit()
    return conn
