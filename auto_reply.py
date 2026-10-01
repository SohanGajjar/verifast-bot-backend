"""Auto-replies to incoming Instagram DMs and comments, driven by reply_configs.

    DM       + 'dm' rule       -> `reply` DMed back into the conversation
    comment  + 'comment' rule  -> `comment_reply` as a public reply under the comment
                                  (mode comment_only), plus `reply` as a private
                                  reply DM when it is set (mode comment_and_dm)

Matching: a rule of the event's type whose `received` equals the incoming text
(trimmed, case-insensitive) wins, otherwise that type's default rule (received NULL).
A dm rule's NULL reply / a comment rule's NULL comment_reply falls back to the
default rule's; a comment rule's NULL reply means no DM. No text -> nothing sent.

Platform rules:
- A session hold (an admin has taken the session over) is read again after the
  claim and before the Instagram call. The attempt is marked skipped and is not
  sent; that claim is what stops release from answering the same message later.
  A call already in flight to Instagram is not cancelled.
- Webhooks may be delivered twice (each delivery gets a fresh requestId). A
  duplicate is normally stopped when it is stored, but every reply is also
  claimed in auto_replies under UNIQUE (kind, source_key) — source_key is the
  comment id / DM message id — and only the claimer sends.
- One private reply per comment, only within 7 days of the comment; Meta
  silently drops extras, so a second one is never attempted.
- DMs only within 24h of the customer's last message in the conversation.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from instagram_service import InstagramServiceError

log = logging.getLogger("bot_backend.auto_reply")

DM_WINDOW = timedelta(hours=24)
PRIVATE_REPLY_WINDOW = timedelta(days=7)

# Keys the gateway / Zernio may use for the id of what was just sent.
_REPLY_ID_KEYS = ("messageId", "message_id", "commentId", "comment_id", "replyId", "id")


def _now():
    return datetime.now(timezone.utc).isoformat()


def session_holder(db, index_name, session_id):
    """Email of the admin holding this session, or None.

    Commits first so a hold written and committed on another connection is
    visible — a read left open would keep seeing the snapshot from before takeover.
    """
    db.commit()
    row = db.execute(
        "SELECT holder_email FROM session_holds WHERE index_name = ? AND session_id = ?",
        (index_name, session_id)).fetchone()
    return row["holder_email"] if row else None


def take_hold(db, index_name, session_id, holder_email, now):
    """Create the hold, or transfer it to `holder_email` if one already exists."""
    existing = db.execute(
        "SELECT 1 FROM session_holds WHERE index_name = ? AND session_id = ?",
        (index_name, session_id)).fetchone()
    if existing:
        db.execute(
            "UPDATE session_holds SET holder_email = ?, updated_at = ? "
            "WHERE index_name = ? AND session_id = ?",
            (holder_email, now, index_name, session_id))
    else:
        try:
            db.execute(
                "INSERT INTO session_holds (index_name, session_id, holder_email, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (index_name, session_id, holder_email, now, now))
        except sqlite3.IntegrityError:
            db.execute(
                "UPDATE session_holds SET holder_email = ?, updated_at = ? "
                "WHERE index_name = ? AND session_id = ?",
                (holder_email, now, index_name, session_id))
    db.commit()


def release_hold(db, index_name, session_id):
    db.execute("DELETE FROM session_holds WHERE index_name = ? AND session_id = ?",
               (index_name, session_id))
    db.commit()


def _norm(text):
    return (text or "").strip().lower()


def find_rule(db, index_name, reply_type, text):
    """(matching rule, default rule) for an incoming `reply_type` ('dm' | 'comment') message.

    The matching rule is the default rule when no `received` matches; either may be None.
    """
    rows = db.execute("SELECT * FROM reply_configs WHERE index_name = ? AND type = ? ORDER BY id",
                      (index_name, reply_type)).fetchall()
    default = next((r for r in rows if r["received"] is None), None)
    key = _norm(text)
    rule = next((r for r in rows if r["received"] is not None and _norm(r["received"]) == key),
                default)
    return rule, default


def plan_replies(rule, default, reply_type):
    """(mode, [(kind, text), ...]) to send for a matched rule."""
    fallback = dict(default) if default else {}
    if reply_type == "dm":
        return "dm", [("dm", rule["reply"] or fallback.get("reply"))]
    mode = "comment_and_dm" if rule["reply"] else "comment_only"
    return mode, [("comment_reply", rule["comment_reply"] or fallback.get("comment_reply")),
                  ("private_reply", rule["reply"])]


def handle_event(db, ig, index_name, meta, request_id):
    """Send the configured auto-replies for one stored webhook event.

    Returns the auto_replies rows this call claimed (duplicates claim nothing).
    """
    event_type = meta.get("instagram_event_type")
    if event_type not in ("dm", "comment"):
        return []
    rule, default = find_rule(db, index_name, event_type, meta.get("user_query"))
    mode, plan = plan_replies(rule, default, event_type) if rule else (None, [])
    plan = [(kind, text) for kind, text in plan if text]
    if not plan:
        log.info("No auto-reply configured for %s", event_type, extra={
            "kind": "auto_reply", "outcome": "no_rule", "index_name": index_name,
            "session_id": meta.get("session_id"), "event_type": event_type})
        return []

    ev = {
        "index_name": index_name,
        "session_id": meta.get("session_id"),
        "event_type": event_type,
        "username": meta.get("customer_username") or "",
        "account_id": meta.get("instagram_account_id") or None,
        "conversation_id": meta.get("instagram_conversation_id") or None,
        "comment_id": meta.get("instagram_comment_id") or None,
        "post_id": meta.get("instagram_post_id") or None,
    }
    if event_type == "dm":
        source_key = meta.get("instagram_message_id") or request_id
    else:
        source_key = ev["comment_id"]
    if not source_key:
        log.warning("Auto-reply skipped: event has no id to dedupe on", extra={
            "kind": "auto_reply", "outcome": "skipped", **ev})
        return []
    sent = (_send(db, ig, kind, rule, mode, text, ev, source_key) for kind, text in plan)
    return [row for row in sent if row]


def _send(db, ig, kind, rule, mode, text, ev, source_key):
    now = _now()
    cur = db.execute(
        "INSERT OR IGNORE INTO auto_replies (index_name, session_id, mode, kind, source_key, "
        "config_id, text, parent_comment_id, post_id, conversation_id, account_id, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (ev["index_name"], ev["session_id"], mode, kind, source_key, rule["id"], text,
         ev["comment_id"], ev["post_id"], ev["conversation_id"], ev["account_id"], now, now),
    )
    db.commit()
    log_extra = {"kind": "auto_reply", "reply_kind": kind, "mode": mode,
                 "config_id": rule["id"], "source_key": source_key, **ev}
    if not cur.rowcount:
        log.info("Auto-reply %s already handled for %s", kind, source_key,
                 extra={**log_extra, "outcome": "duplicate"})
        return None
    row_id = cur.lastrowid

    reason = outside_window(db, kind, ev)
    if reason:
        log.info("Auto-reply %s skipped: %s", kind, reason,
                 extra={**log_extra, "outcome": "skipped"})
        return _finish(db, row_id, "skipped", error=reason)
    # Takeover can commit after this event was queued. Read the hold now, not
    # at enqueue. A send already inside the Instagram call below is not cancelled.
    holder = session_holder(db, ev["index_name"], ev["session_id"])
    if holder:
        log.info("Auto-reply %s skipped: session held by %s", kind, holder,
                 extra={**log_extra, "outcome": "skipped", "holder_email": holder})
        return _finish(db, row_id, "skipped", error="held")
    try:
        if kind == "dm":
            result = ig.send_msg(ev["conversation_id"], text, account_id=ev["account_id"])
        elif kind == "comment_reply":
            result = ig.reply_comment(ev["comment_id"], text, account_id=ev["account_id"])
        else:
            result = ig.private_reply(ev["post_id"], ev["comment_id"], text,
                                      account_id=ev["account_id"])
    except (InstagramServiceError, ValueError) as e:
        log.error("Auto-reply %s failed: %s", kind, e, extra={**log_extra, "outcome": "failed"})
        return _finish(db, row_id, "failed", error=str(e))

    reply_id = reply_id_from(result)
    _record_chat(db, ev, text, kind, reply_id)
    log.info("Auto-reply %s sent", kind, extra={**log_extra, "outcome": "sent"})
    return _finish(db, row_id, "sent", reply_id=reply_id)


def outside_window(db, kind, ev):
    """Why `kind` may not be sent right now, or None.

    `ev` needs index_name, conversation_id, comment_id and post_id.
    """
    if kind == "dm":
        if not ev["conversation_id"]:
            return "missing conversation_id"
        last = db.execute(
            "SELECT MAX(created_at) FROM chat_history WHERE index_name = ? "
            "AND conversation_id = ? AND actor = 'customer'",
            (ev["index_name"], ev["conversation_id"])).fetchone()[0]
        if not last or _age(last) > DM_WINDOW:
            return "outside the 24h DM window"
    elif kind == "private_reply":
        if not (ev["post_id"] and ev["comment_id"]):
            return "missing post_id / comment_id"
        first = db.execute(
            "SELECT MIN(created_at) FROM chat_history WHERE comment_id = ? AND actor = 'customer'",
            (ev["comment_id"],)).fetchone()[0]
        if not first or _age(first) > PRIVATE_REPLY_WINDOW:
            return "outside the 7-day private reply window"
    elif not ev["comment_id"]:
        return "missing comment_id"
    return None


def _age(iso):
    return datetime.now(timezone.utc) - datetime.fromisoformat(iso)


def reply_id_from(result):
    """Instagram id of what a gateway send just created (None when absent)."""
    if not isinstance(result, dict):
        return None
    for body in (result, *(result.get(k) for k in ("data", "message", "comment", "reply"))):
        if isinstance(body, dict):
            for key in _REPLY_ID_KEYS:
                if body.get(key):
                    return str(body[key])
    return None


def _finish(db, row_id, status, reply_id=None, error=None):
    db.execute("UPDATE auto_replies SET status = ?, reply_id = ?, error = ?, updated_at = ? "
               "WHERE id = ?", (status, reply_id, error, _now(), row_id))
    db.commit()
    return dict(db.execute("SELECT * FROM auto_replies WHERE id = ?", (row_id,)).fetchone())


def _record_chat(db, ev, text, reply_kind, reply_id):
    """Show the auto-reply in the inbox as an outbound 'ai' message."""
    db.execute(
        "INSERT INTO chat_history (index_name, session_id, actor, text, event_type, username, "
        "created_at, account_id, conversation_id, comment_id, post_id, reply_kind, reply_id) "
        "VALUES (?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ev["index_name"], ev["session_id"], text, ev["event_type"], ev["username"], _now(),
         ev["account_id"] or "", ev["conversation_id"] or "", ev["comment_id"] or "",
         ev["post_id"] or "", reply_kind, reply_id),
    )
    db.commit()
