"""Customer edits of a DM (Zernio's message.edited webhook).

An edit is not a new message — the customer changed one they already sent. It is
applied to that message's chat_history row in place: text becomes the latest
version and edited_at is set. The version it replaced is kept in message_edits,
so the screen can show the earlier versions. Edits are never auto-replied to: the
bot answered the message when it arrived.

The message is found by Instagram's id for it (chat_history.message_id). Rows
stored before that column existed have none; for those the edit is matched to the
customer's latest DM in the conversation whose text is the edit's previous
version, and the row's message_id is backfilled so later edits match directly.

Deliveries are idempotent: an edit whose text is already the row's text
('duplicate'), or that is older than the row's last edit ('stale'), changes
nothing. An edit of a message the inbox never received is 'ignored'.
"""
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _utc_iso(ts):
    """`ts` (ISO 8601, any offset) in UTC, formatted like _now() so edits order as
    strings; now when it's missing or unparseable."""
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return _now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _find_message(db, index_name, meta):
    message_id = meta.get("instagram_message_id")
    if not message_id:
        return None
    row = db.execute(
        "SELECT * FROM chat_history WHERE index_name = ? AND message_id = ? "
        "AND actor = 'customer' ORDER BY id LIMIT 1", (index_name, message_id)).fetchone()
    if row or not meta.get("previous_text"):
        return row
    row = db.execute(
        "SELECT * FROM chat_history WHERE index_name = ? AND session_id = ? "
        "AND actor = 'customer' AND event_type = 'dm' AND IFNULL(message_id, '') = '' "
        "AND IFNULL(conversation_id, '') = ? AND text = ? ORDER BY id DESC LIMIT 1",
        (index_name, meta.get("session_id"), meta.get("instagram_conversation_id") or "",
         meta["previous_text"])).fetchone()
    if not row:
        return None
    db.execute("UPDATE chat_history SET message_id = ? WHERE id = ?", (message_id, row["id"]))
    return db.execute("SELECT * FROM chat_history WHERE id = ?", (row["id"],)).fetchone()


def apply_edit(db, index_name, meta, request_id):
    """Apply one message.edited delivery. Returns (status, chat_history row or None),
    status 'edited' | 'duplicate' | 'stale' | 'ignored'."""
    row = _find_message(db, index_name, meta)
    if not row:
        return "ignored", None
    text = meta.get("user_query") or ""
    edited_at = _utc_iso(meta.get("edited_at"))
    previous = row["text"] or ""
    if text == previous:
        status = "duplicate"
    elif row["edited_at"] and edited_at <= row["edited_at"]:
        status = "stale"
    else:
        # Compare-and-swap on the text read above: of two deliveries racing, one wins.
        status = "edited" if db.execute(
            "UPDATE chat_history SET text = ?, edited_at = ? WHERE id = ? AND text IS ? "
            "AND edited_at IS ?", (text, edited_at, row["id"], row["text"], row["edited_at"]),
        ).rowcount else "duplicate"
    if status == "edited":
        db.execute(
            "INSERT INTO message_edits (index_name, session_id, message_id, history_id, "
            "previous_text, text, edited_at, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (index_name, row["session_id"], row["message_id"], row["id"], previous, text,
             edited_at, request_id, _now()))
    db.commit()
    return status, db.execute("SELECT * FROM chat_history WHERE id = ?", (row["id"],)).fetchone()


def attach_edit_history(db, index_name, rows):
    """Set edit_history on each GET /chat/history row: the earlier versions of an
    edited DM, oldest first, as {text, created_at} — created_at being when that
    version was written (the message's arrival, then each earlier edit)."""
    edited = sorted({r["message_id"] for r in rows if r.get("edited_at") and r.get("message_id")})
    chains = {}
    if edited:
        for e in db.execute(
                "SELECT message_id, previous_text, edited_at FROM message_edits "
                f"WHERE index_name = ? AND message_id IN ({', '.join('?' * len(edited))}) "
                "ORDER BY edited_at, id", (index_name, *edited)):
            chains.setdefault(e["message_id"], []).append(e)
    for r in rows:
        written, versions = r["created_at"], []
        for e in chains.get(r.get("message_id"), []) if r.get("edited_at") else []:
            versions.append({"text": e["previous_text"], "created_at": written})
            written = e["edited_at"]
        r["edit_history"] = versions
    return rows
