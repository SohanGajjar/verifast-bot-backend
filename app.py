"""Minimal receiver (:9000) connecting the Instagram gateway to site-frontend.

- POST /events                                  ← channel-integration gateway
  New DMs / comments trigger the configured auto-replies in the background
  (see auto_reply.py); every attempt is persisted in auto_replies.
- GET  /chat/instagram/<index>/channel-check    → frontend: show the Instagram inbox
- GET  /chat/instagram/<index>/sessions         → frontend: conversation list
- GET  /chat/history/<index>/<session_id>       → frontend: messages in a conversation
- GET  /chat/instagram/<index>/<session_id>/context
- POST /chat/instagram/<index>/<session_id>/send-message           {text}
- POST /chat/instagram/<index>/<session_id>/reply-comment          {text, comment_id?}
- POST /chat/instagram/<index>/<session_id>/private-reply          {text, comment_id?}
- POST /chat/instagram/<index>/<session_id>/comments/<id>/hide|unhide
- DELETE /chat/instagram/<index>/<session_id>/comments/<id>
  Outbound actions go through InstagramService → gateway /actions/*; replies
  are stored as actor "agent". comment_id defaults to the session's latest.
- GET    /chat/instagram/<index>/reply-configs[?type=dm|comment]
- POST   /chat/instagram/<index>/reply-configs        {type, received?, reply?, comment_reply?}
- PUT    /chat/instagram/<index>/reply-configs/<id>   {type?, received?, reply?, comment_reply?}
- DELETE /chat/instagram/<index>/reply-configs/<id>
  Auto-reply rules per client; received = null means "any message" (default).
  dm rules: reply is the DM (null = default reply). comment rules:
  comment_reply is the public reply (null = default), reply is the private
  reply DM — null means public reply only, no DM.
- GET    /chat/instagram/<index>/auto-replies[?session_id=&status=]
  Sent / skipped / failed auto-replies, newest first.

Data lives in app.db (chat_history, webhook_events, reply_configs,
auto_replies) — see database.py.

Run with the gateway's venv (it already has flask + flask-cors):
    /tmp/ci-venv/bin/python app.py

Requests, responses, webhook events and gateway calls are logged as JSON lines
to logs/bot-backend.log — see logging_service.py.
"""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS

import auto_reply
import database
import logging_service
from instagram_service import InstagramService, InstagramServiceError

log = logging_service.get_logger("bot_backend.app")
webhook_log = logging_service.webhook_log

app = Flask(__name__)
CORS(app)
logging_service.init_app(app)

ig = InstagramService(
    base_url=os.getenv("INSTAGRAM_GATEWAY_URL", "http://localhost:8082"),
    internal_key=os.getenv("INTERNAL_API_KEY", "dev-internal-key"),
    account_id=os.getenv("INSTAGRAM_ACCOUNT_ID") or None,
)

db = database.connect()
# Auto-replies run off the request thread (the gateway times out after 10s) on a
# single worker with its own connection, so sends are serialized.
auto_reply_db = database.connect()
auto_reply_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="auto-reply")
# Ids needed to act on a session.
TARGET_COLUMNS = ("account_id", "conversation_id", "comment_id", "post_id")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _record_webhook(em, meta, status):
    db.execute(
        "INSERT INTO webhook_events (request_id, index_name, session_id, event_type, status, "
        "payload, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (em.get("requestId"), em.get("index_name") or meta.get("index_name"),
         meta.get("session_id"), meta.get("instagram_event_type"), status,
         request.get_data(as_text=True), _now()),
    )
    db.commit()


@app.post("/events")
def events():
    em = (request.get_json(silent=True) or {}).get("event_metadata") or {}
    meta = (em.get("original_payload") or {}).get("metadata") or {}
    event = {
        "kind": "webhook_event",
        "event_request_id": em.get("requestId"),
        "index_name": em.get("index_name") or meta.get("index_name"),
        "session_id": meta.get("session_id"),
        "event_type": meta.get("instagram_event_type"),
        "account_id": meta.get("instagram_account_id"),
        "conversation_id": meta.get("instagram_conversation_id"),
        "comment_id": meta.get("instagram_comment_id"),
        "post_id": meta.get("instagram_post_id"),
        "username": meta.get("customer_username"),
        "text": logging_service.truncate(meta.get("user_query")),
    }
    if not meta.get("session_id"):
        webhook_log.warning("Webhook event ignored: missing session_id",
                            extra={**event, "outcome": "ignored"})
        _record_webhook(em, meta, "ignored")
        return jsonify({"status": "ignored"}), 400
    cur = db.execute(
        "INSERT OR IGNORE INTO chat_history (request_id, index_name, session_id, actor, text, "
        "event_type, username, created_at, account_id, conversation_id, comment_id, post_id) "
        "VALUES (?, ?, ?, 'customer', ?, ?, ?, ?, ?, ?, ?, ?)",
        (em.get("requestId"), em.get("index_name") or meta.get("index_name"),
         meta["session_id"], meta.get("user_query", ""), meta.get("instagram_event_type"),
         meta.get("customer_username", ""), _now(),
         meta.get("instagram_account_id", ""), meta.get("instagram_conversation_id", ""),
         meta.get("instagram_comment_id", ""), meta.get("instagram_post_id", "")),
    )
    db.commit()
    outcome = "stored" if cur.rowcount else "duplicate"
    _record_webhook(em, meta, outcome)
    webhook_log.info("Webhook event %s: %s", outcome, event["event_type"],
                     extra={**event, "outcome": outcome})
    if outcome == "stored":
        auto_reply_worker.submit(_auto_reply, event["index_name"], meta, em.get("requestId"))
    return jsonify({"status": "stored"})


def _auto_reply(index_name, meta, request_id):
    try:
        auto_reply.handle_event(auto_reply_db, ig, index_name, meta, request_id)
    except Exception:
        log.exception("Auto-reply crashed", extra={"kind": "auto_reply", "outcome": "error",
                                                   "session_id": meta.get("session_id")})


@app.get("/chat/instagram/<index_name>/channel-check")
def channel_check(index_name):
    return jsonify({"index_name": index_name, "is_instagram": True, "channels": ["instagram"]})


@app.get("/chat/instagram/<index_name>/sessions")
def sessions(index_name):
    start = request.args.get("startDate", "0000")
    end = request.args.get("endDate", "9999")
    rows = db.execute(
        "SELECT m.*, s.first_id FROM chat_history m JOIN ("
        "  SELECT session_id, MIN(id) AS first_id, MAX(id) AS last_id FROM chat_history"
        "  WHERE index_name = ? GROUP BY session_id) s ON m.id = s.last_id "
        "WHERE m.created_at BETWEEN ? AND ? ORDER BY m.id DESC",
        (index_name, start, end),
    ).fetchall()
    return jsonify({"sessions": [{
        "session_id": r["session_id"],
        "session_number": r["first_id"],
        "index_name": index_name,
        "channel": "instagram",
        "market": "",
        "session_user_state": None,
        "ig_event_type": r["event_type"],
        "instagram_user_id": r["username"] or None,
        "asset": None,
        "last_message_preview": r["text"][:140],
        "last_message_at": r["created_at"],
        "last_message_actor": r["actor"],
        "user_fields": [],
        "use_cases": [],
        "chat_interactions": [],
    } for r in rows]})


HISTORY_COLUMNS = (
    "id, actor, text, index_name, session_id AS session, created_at, event_type, reply_kind, "
    "comment_id"
)


@app.get("/chat/history/<index_name>/<session_id>")
def history(index_name, session_id):
    rows = db.execute(
        f"SELECT {HISTORY_COLUMNS} FROM chat_history "
        "WHERE index_name = ? AND session_id = ? ORDER BY id", (index_name, session_id),
    ).fetchall()
    return jsonify([dict(r) for r in rows])



@app.errorhandler(InstagramServiceError)
def instagram_error(e):
    log.error("Gateway action failed: %s", e, extra={
        "kind": "gateway_error", "status_code": e.status_code,
        "details": logging_service.body_for_log(e.body)})
    return jsonify({"error": str(e), "status_code": e.status_code, "details": e.body}), 502


@app.errorhandler(ValueError)
def bad_value(e):
    log.warning("Bad request: %s", e, extra={"kind": "validation_error"})
    return jsonify({"error": str(e)}), 400


def _latest(index_name, session_id, column, value=None):
    """Most recent customer message in the session with `column` set (or == value)."""
    sql = (f"SELECT * FROM chat_history WHERE index_name = ? AND session_id = ? "
           f"AND actor = 'customer' AND {column} IS NOT NULL AND {column} != ''")
    args = [index_name, session_id]
    if value:
        sql += f" AND {column} = ?"
        args.append(value)
    return db.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def _text():
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    return text


def _record_agent(target, text, reply_kind):
    cur = db.execute(
        "INSERT INTO chat_history (index_name, session_id, actor, text, event_type, username, "
        "created_at, account_id, conversation_id, comment_id, post_id, reply_kind) "
        "VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (target["index_name"], target["session_id"], text, target["event_type"],
         target["username"], _now(),
         *(target[c] for c in TARGET_COLUMNS), reply_kind),
    )
    db.commit()
    return dict(db.execute(
        f"SELECT {HISTORY_COLUMNS} FROM chat_history WHERE id = ?", (cur.lastrowid,)).fetchone())


def _not_found(what):
    return jsonify({"error": f"no {what} found for this session"}), 404


@app.get("/chat/instagram/<index_name>/<session_id>/context")
def context(index_name, session_id):
    last = db.execute(
        "SELECT event_type, username FROM chat_history WHERE index_name = ? AND session_id = ? "
        "AND actor = 'customer' ORDER BY id DESC LIMIT 1", (index_name, session_id),
    ).fetchone()
    if not last:
        return _not_found("messages")
    return jsonify({"index_name": index_name, "session_id": session_id,
                    "ig_event_type": last["event_type"],
                    "instagram_user_id": last["username"] or None, "asset": None})


@app.post("/chat/instagram/<index_name>/<session_id>/send-message")
def send_message(index_name, session_id):
    text = _text()
    target = _latest(index_name, session_id, "conversation_id")
    if not target:
        return _not_found("DM conversation")
    result = ig.send_msg(target["conversation_id"], text, account_id=target["account_id"])
    return jsonify({"status": "sent", "message": _record_agent(target, text, "dm"),
                    "result": result})


@app.post("/chat/instagram/<index_name>/<session_id>/reply-comment")
def reply_comment(index_name, session_id):
    text = _text()
    comment_id = (request.get_json(silent=True) or {}).get("comment_id")
    target = _latest(index_name, session_id, "comment_id", comment_id)
    if not target:
        return _not_found("comment")
    result = ig.reply_comment(target["comment_id"], text, account_id=target["account_id"])
    return jsonify({"status": "sent", "message": _record_agent(target, text, "comment_reply"),
                    "result": result})


@app.post("/chat/instagram/<index_name>/<session_id>/private-reply")
def private_reply(index_name, session_id):
    text = _text()
    comment_id = (request.get_json(silent=True) or {}).get("comment_id")
    target = _latest(index_name, session_id, "comment_id", comment_id)
    if not target:
        return _not_found("comment")
    result = ig.private_reply(target["post_id"], target["comment_id"], text,
                              account_id=target["account_id"])
    return jsonify({"status": "sent", "message": _record_agent(target, text, "private_reply"),
                    "result": result})


@app.post("/chat/instagram/<index_name>/<session_id>/comments/<comment_id>/<action>")
def moderate_comment(index_name, session_id, comment_id, action):
    if action not in ("hide", "unhide"):
        return jsonify({"error": "action must be hide or unhide"}), 404
    target = _latest(index_name, session_id, "comment_id", comment_id)
    if not target:
        return _not_found("comment")
    fn = ig.hide_comment if action == "hide" else ig.unhide_comment
    result = fn(comment_id, account_id=target["account_id"])
    return jsonify({"status": "hidden" if action == "hide" else "unhidden",
                    "comment_id": comment_id, "result": result})


@app.delete("/chat/instagram/<index_name>/<session_id>/comments/<comment_id>")
def delete_comment(index_name, session_id, comment_id):
    target = _latest(index_name, session_id, "comment_id", comment_id)
    if not target:
        return _not_found("comment")
    result = ig.delete_comment(comment_id, account_id=target["account_id"])
    return jsonify({"status": "deleted", "comment_id": comment_id, "result": result})


def _reply_config_fields(body, current=None):
    """Validate a reply-config body — a partial one when updating the `current` row.

    Blank text fields are stored as NULL. comment_reply only applies to comment
    rules; switching a rule to dm clears it.
    """
    fields = {}
    if current is None or "type" in body:
        if body.get("type") not in database.REPLY_TYPES:
            raise ValueError("type must be 'dm' or 'comment'")
        fields["type"] = body["type"]
    for key in ("received", "reply", "comment_reply"):
        if current is None or key in body:
            value = body.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a string or null")
            fields[key] = (value or "").strip() or None
    if fields.get("type", current and current["type"]) == "dm":
        if fields.get("comment_reply"):
            raise ValueError("comment_reply only applies to comment rules")
        fields["comment_reply"] = None
    return fields


def _reply_config(index_name, config_id):
    row = db.execute("SELECT * FROM reply_configs WHERE index_name = ? AND id = ?",
                     (index_name, config_id)).fetchone()
    return dict(row) if row else None


def _duplicate_rule():
    return jsonify({"error": "a reply config for this type and received text already exists"}), 409


def _reply_config_not_found():
    return jsonify({"error": "reply config not found"}), 404


@app.get("/chat/instagram/<index_name>/reply-configs")
def list_reply_configs(index_name):
    reply_type = request.args.get("type")
    sql, args = "SELECT * FROM reply_configs WHERE index_name = ?", [index_name]
    if reply_type:
        sql += " AND type = ?"
        args.append(reply_type)
    # Specific rules first, then the default (received IS NULL) for each type.
    rows = db.execute(sql + " ORDER BY type, received IS NULL, id", args).fetchall()
    return jsonify({"reply_configs": [dict(r) for r in rows]})


@app.post("/chat/instagram/<index_name>/reply-configs")
def create_reply_config(index_name):
    fields = _reply_config_fields(request.get_json(silent=True) or {})
    now = _now()
    try:
        cur = db.execute(
            "INSERT INTO reply_configs (index_name, type, received, reply, comment_reply, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (index_name, fields["type"], fields["received"], fields["reply"],
             fields["comment_reply"], now, now),
        )
    except sqlite3.IntegrityError:
        return _duplicate_rule()
    db.commit()
    return jsonify(_reply_config(index_name, cur.lastrowid)), 201


@app.put("/chat/instagram/<index_name>/reply-configs/<int:config_id>")
def update_reply_config(index_name, config_id):
    current = _reply_config(index_name, config_id)
    if not current:
        return _reply_config_not_found()
    fields = _reply_config_fields(request.get_json(silent=True) or {}, current)
    if fields:
        assignments = ", ".join(f"{k} = ?" for k in fields)
        try:
            db.execute(f"UPDATE reply_configs SET {assignments}, updated_at = ? WHERE id = ?",
                       (*fields.values(), _now(), config_id))
        except sqlite3.IntegrityError:
            return _duplicate_rule()
        db.commit()
    return jsonify(_reply_config(index_name, config_id))


@app.delete("/chat/instagram/<index_name>/reply-configs/<int:config_id>")
def delete_reply_config(index_name, config_id):
    cur = db.execute("DELETE FROM reply_configs WHERE index_name = ? AND id = ?",
                     (index_name, config_id))
    db.commit()
    if not cur.rowcount:
        return _reply_config_not_found()
    return jsonify({"status": "deleted", "id": config_id})


@app.get("/chat/instagram/<index_name>/auto-replies")
def list_auto_replies(index_name):
    sql, args = "SELECT * FROM auto_replies WHERE index_name = ?", [index_name]
    for key in ("session_id", "status"):
        if request.args.get(key):
            sql += f" AND {key} = ?"
            args.append(request.args[key])
    rows = db.execute(sql + " ORDER BY id DESC", args).fetchall()
    return jsonify({"auto_replies": [dict(r) for r in rows]})


if __name__ == "__main__":
    log.info("bot-backend starting", extra={"port": 9000, "log_dir": logging_service.LOG_DIR})
    app.run(host="0.0.0.0", port=9000)
