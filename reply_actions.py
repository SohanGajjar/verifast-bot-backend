"""Agent actions on Instagram replies from the chat screen.

Messages are addressed by their chat_history id (what the screen already has).

Edit — Instagram cannot edit a published comment, so an edit of a public comment
reply is a *replacement*:
    1. claim the original in reply_edits (UNIQUE original_id), so a double-click or
       a second tab cannot post two replacements;
    2. post the new text as a reply under the same parent comment;
    3. delete the original reply.
Posting first means a failure never leaves the customer with no answer: if the
post fails nothing changed (status 'failed', the edit may be retried); if only the
delete fails, both replies are live (status 'partial') and the original stays
deletable from the screen. The replacement is a new comment — it gets a new id,
a new timestamp and sorts last in the thread, and likes on the original are lost.

Delete — only comments can be deleted (there is no API to unsend a DM or a
private reply): the bot's / an agent's public reply, or the customer's comment.
Deleting the customer's comment also marks the replies under it deleted, since
Instagram removes a comment's replies with it. Deleting is idempotent locally.

Private reply — Instagram allows one per comment, within 7 days of it, and
silently drops extras. The send claims the same auto_replies (kind, source_key)
lock the auto-reply uses, so the bot and an agent can never both send one.
"""
from datetime import datetime, timezone

import auto_reply
from instagram_service import InstagramServiceError

OUTBOUND_ACTORS = ("ai", "agent")

TARGET_COLUMNS = ("account_id", "conversation_id", "comment_id", "post_id")

# What GET /chat/history returns per message. replaced_by: the newer reply that
# replaced this one through an edit. message_id / edited_at: a customer DM and
# when the customer last edited it (its earlier versions: message_edits.py).
HISTORY_COLUMNS = (
    "id, actor, text, index_name, session_id AS session, created_at, event_type, reply_kind, "
    "comment_id, reply_id, deleted_at, replaces_id, message_id, edited_at, "
    "(SELECT n.id FROM chat_history n WHERE n.replaces_id = chat_history.id "
    " ORDER BY n.id DESC LIMIT 1) AS replaced_by"
)


class ActionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def history_row(db, row_id):
    row = db.execute(f"SELECT {HISTORY_COLUMNS} FROM chat_history WHERE id = ?",
                     (row_id,)).fetchone()
    return dict(row) if row else None


def record_outbound(db, source, actor, text, reply_kind, reply_id=None, replaces_id=None):
    """Store a sent reply in chat_history, copying the targets from `source` (a
    chat_history row of the same conversation / comment). Returns the history row."""
    cur = db.execute(
        "INSERT INTO chat_history (index_name, session_id, actor, text, event_type, username, "
        "created_at, account_id, conversation_id, comment_id, post_id, reply_kind, reply_id, "
        "replaces_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source["index_name"], source["session_id"], actor, text, source["event_type"],
         source["username"], _now(), *(source[c] for c in TARGET_COLUMNS),
         reply_kind, reply_id, replaces_id),
    )
    db.commit()
    return history_row(db, cur.lastrowid)


def window_target(row):
    """The `ev` auto_reply.outside_window() checks a send against."""
    return {"index_name": row["index_name"], "conversation_id": row["conversation_id"] or None,
            "comment_id": row["comment_id"] or None, "post_id": row["post_id"] or None}


def check_window(db, kind, row):
    reason = auto_reply.outside_window(db, kind, window_target(row))
    if reason:
        raise ActionError(f"Instagram won't deliver this: {reason}", 422)


def get_message(db, index_name, session_id, message_id):
    row = db.execute("SELECT * FROM chat_history WHERE index_name = ? AND session_id = ? "
                     "AND id = ?", (index_name, session_id, message_id)).fetchone()
    if not row:
        raise ActionError("message not found in this conversation", 404)
    return row


def _account(row):
    return row["account_id"] or None


def _live_comment_reply(row):
    """Raise unless `row` is a public comment reply that is still on Instagram."""
    if row["actor"] not in OUTBOUND_ACTORS or row["reply_kind"] != "comment_reply":
        raise ActionError("only public comment replies can be edited")
    if row["deleted_at"]:
        raise ActionError("this reply was already deleted from Instagram", 409)
    if not row["reply_id"]:
        raise ActionError("the Instagram id of this reply is unknown, so it can't be changed", 409)


# --- edit ---

def _claim_edit(db, row, text):
    now = _now()
    claimed = db.execute(
        "INSERT OR IGNORE INTO reply_edits (index_name, session_id, original_id, text, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (row["index_name"], row["session_id"], row["id"], text, now, now)).rowcount
    if not claimed:  # a failed edit posted nothing, so it may be retried
        claimed = db.execute(
            "UPDATE reply_edits SET status = 'pending', text = ?, error = NULL, updated_at = ? "
            "WHERE original_id = ? AND status = 'failed'", (text, now, row["id"])).rowcount
    db.commit()
    if not claimed:
        status = db.execute("SELECT status FROM reply_edits WHERE original_id = ?",
                            (row["id"],)).fetchone()["status"]
        if status == "pending":
            raise ActionError("this reply is already being edited", 409)
        raise ActionError("this reply was already edited — edit the newer reply instead", 409)


def _finish_edit(db, original_id, status, replacement_id=None, error=None):
    db.execute("UPDATE reply_edits SET status = ?, replacement_id = IFNULL(?, replacement_id), "
               "error = ?, updated_at = ? WHERE original_id = ?",
               (status, replacement_id, error, _now(), original_id))
    db.commit()


def edit_reply(db, ig, index_name, session_id, message_id, text):
    row = get_message(db, index_name, session_id, message_id)
    _live_comment_reply(row)
    if text == (row["text"] or "").strip():
        raise ActionError("the new text is the same as the current reply")
    _claim_edit(db, row, text)

    try:
        result = ig.reply_comment(row["comment_id"], text, account_id=_account(row))
    except (InstagramServiceError, ValueError) as e:
        _finish_edit(db, row["id"], "failed", error=str(e))
        raise
    replacement = record_outbound(db, row, "agent", text, "comment_reply",
                                  auto_reply.reply_id_from(result), replaces_id=row["id"])

    try:
        ig.delete_comment(row["reply_id"], account_id=_account(row))
    except (InstagramServiceError, ValueError) as e:
        _finish_edit(db, row["id"], "partial", replacement["id"], str(e))
        return {"status": "partial", "message": replacement,
                "original": history_row(db, row["id"]),
                "error": f"the new reply was posted, but deleting the original failed: {e}"}
    _mark_deleted(db, "id = ?", (row["id"],))
    _finish_edit(db, row["id"], "done", replacement["id"])
    return {"status": "edited", "message": replacement, "original": history_row(db, row["id"])}


# --- delete ---

def _mark_deleted(db, where, args):
    cur = db.execute(f"UPDATE chat_history SET deleted_at = ? WHERE deleted_at IS NULL AND {where}",
                     (_now(), *args))
    db.commit()
    return cur.rowcount


def delete_message(db, ig, index_name, session_id, message_id):
    row = get_message(db, index_name, session_id, message_id)
    if row["deleted_at"]:
        return {"status": "already_deleted", "message": history_row(db, row["id"])}
    is_customer_comment = row["actor"] == "customer" and row["event_type"] == "comment"
    if is_customer_comment:
        if not row["comment_id"]:
            raise ActionError("the Instagram id of this comment is unknown, so it can't be deleted",
                              409)
        instagram_id = row["comment_id"]
    elif row["actor"] in OUTBOUND_ACTORS and row["reply_kind"] == "comment_reply":
        if not row["reply_id"]:
            raise ActionError("the Instagram id of this reply is unknown, so it can't be deleted",
                              409)
        instagram_id = row["reply_id"]
    else:
        raise ActionError("only comments can be deleted — Instagram has no API to unsend a DM "
                          "or a private reply")

    ig.delete_comment(instagram_id, account_id=_account(row))

    scope = "index_name = ? AND session_id = ?"
    if is_customer_comment:
        # The comment plus the replies under it.
        removed = _mark_deleted(
            db, f"{scope} AND comment_id = ? AND (actor = 'customer' OR reply_kind = 'comment_reply')",
            (index_name, session_id, instagram_id))
    else:
        removed = _mark_deleted(db, "id = ?", (row["id"],))
        # Finishes an edit whose original could not be deleted at the time.
        db.execute("UPDATE reply_edits SET status = 'done', error = NULL, updated_at = ? "
                   "WHERE original_id = ? AND status = 'partial'", (_now(), row["id"]))
        db.commit()
    return {"status": "deleted", "message": history_row(db, row["id"]), "removed": removed}


def find_comment_message(db, index_name, session_id, instagram_id):
    """chat_history id of the customer comment or comment reply with this Instagram id."""
    row = db.execute(
        "SELECT id FROM chat_history WHERE index_name = ? AND session_id = ? AND ("
        "(actor = 'customer' AND event_type = 'comment' AND comment_id = ?) OR "
        "(reply_kind = 'comment_reply' AND reply_id = ?)) ORDER BY id DESC LIMIT 1",
        (index_name, session_id, instagram_id, instagram_id)).fetchone()
    if not row:
        raise ActionError("comment not found in this conversation", 404)
    return row["id"]


# --- private reply ---

def _claim_private_reply(db, target, text):
    now = _now()
    values = (target["index_name"], target["session_id"], target["comment_id"], text,
              target["comment_id"], target["post_id"] or None,
              target["conversation_id"] or None, target["account_id"] or None, now, now)
    cur = db.execute(
        "INSERT OR IGNORE INTO auto_replies (index_name, session_id, mode, kind, source_key, "
        "text, parent_comment_id, post_id, conversation_id, account_id, status, created_at, "
        "updated_at) VALUES (?, ?, 'agent', 'private_reply', ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        values)
    if not cur.rowcount:  # retry a failed / skipped one; a sent or pending one is final
        cur = db.execute(
            "UPDATE auto_replies SET mode = 'agent', text = ?, status = 'pending', error = NULL, "
            "reply_id = NULL, updated_at = ? WHERE kind = 'private_reply' AND source_key = ? "
            "AND status IN ('failed', 'skipped')", (text, now, target["comment_id"]))
    db.commit()
    if not cur.rowcount:
        raise ActionError("a private reply was already sent for this comment — Instagram allows "
                          "only one", 409)
    return db.execute("SELECT id FROM auto_replies WHERE kind = 'private_reply' "
                      "AND source_key = ?", (target["comment_id"],)).fetchone()["id"]


def _finish_private_reply(db, claim_id, status, reply_id=None, error=None):
    db.execute("UPDATE auto_replies SET status = ?, reply_id = ?, error = ?, updated_at = ? "
               "WHERE id = ?", (status, reply_id, error, _now(), claim_id))
    db.commit()


def send_private_reply(db, ig, target, text):
    """Private reply (DM) to the commenter of `target` (the customer comment row)."""
    if target["deleted_at"]:
        raise ActionError("this comment was deleted", 409)
    already = db.execute(
        "SELECT 1 FROM chat_history WHERE comment_id = ? AND reply_kind = 'private_reply'",
        (target["comment_id"],)).fetchone()
    if already:
        raise ActionError("a private reply was already sent for this comment — Instagram allows "
                          "only one", 409)
    claim_id = _claim_private_reply(db, target, text)
    reason = auto_reply.outside_window(db, "private_reply", window_target(target))
    if reason:
        _finish_private_reply(db, claim_id, "skipped", error=reason)
        raise ActionError(f"Instagram won't deliver this: {reason}", 422)
    try:
        result = ig.private_reply(target["post_id"], target["comment_id"], text,
                                  account_id=_account(target))
    except (InstagramServiceError, ValueError) as e:
        _finish_private_reply(db, claim_id, "failed", error=str(e))
        raise
    reply_id = auto_reply.reply_id_from(result)
    _finish_private_reply(db, claim_id, "sent", reply_id)
    return record_outbound(db, target, "agent", text, "private_reply", reply_id), result
