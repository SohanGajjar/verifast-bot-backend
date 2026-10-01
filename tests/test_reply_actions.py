"""Task 2 — reply actions from the chat screen, against a fake gateway.

    .venv/bin/python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_tmp = tempfile.TemporaryDirectory()
os.environ["APP_DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["LOG_DIR"] = _tmp.name
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import app as app_module  # noqa: E402  (the env above must be set first)
import auto_reply  # noqa: E402
import database  # noqa: E402
from instagram_service import InstagramServiceError  # noqa: E402

INDEX, SESSION = "demo-store", "cust1"
BASE = f"/chat/instagram/{INDEX}/{SESSION}"
COMMENT, POST, CONV, ACCOUNT = "c-100", "p-1", "conv-1", "acc-1"


class FakeGateway:
    """Stands in for InstagramService; responses mirror real gateway bodies."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self._n = 0

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise InstagramServiceError(f"{name} returned 500", status_code=500)

    def _id(self):
        self._n += 1
        return f"new-{self._n}"

    def reply_comment(self, comment_id, text, account_id=None):
        self._call("reply_comment", comment_id, text)
        return {"data": {"commentId": self._id(), "isReply": True}, "success": True}

    def delete_comment(self, comment_id, account_id=None):
        self._call("delete_comment", comment_id)
        return {"success": True}

    def private_reply(self, post_id, comment_id, text, account_id=None):
        self._call("private_reply", post_id, comment_id, text)
        return {"commentId": comment_id, "messageId": self._id(), "status": "success"}

    def send_msg(self, conversation_id, text, account_id=None):
        self._call("send_msg", conversation_id, text)
        return {"data": {"conversationId": conversation_id, "messageId": self._id()},
                "success": True}


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.db = app_module.db
        for table in ("chat_history", "auto_replies", "reply_edits", "session_holds",
                      "message_edits", "webhook_events"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()
        self.ig = FakeGateway()
        app_module.ig = self.ig
        self.client = app_module.app.test_client()

    # --- fixtures ---

    def insert(self, actor, event_type, text, created_at=None, session=SESSION, **cols):
        row = {"index_name": INDEX, "session_id": session, "actor": actor, "text": text,
               "event_type": event_type, "username": "cust", "account_id": ACCOUNT,
               "created_at": created_at or ago(minutes=1), **cols}
        cur = self.db.execute(f"INSERT INTO chat_history ({', '.join(row)}) "
                              f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        self.db.commit()
        return cur.lastrowid

    def comment(self, created_at=None, comment_id=COMMENT):
        return self.insert("customer", "comment", "hi", created_at, comment_id=comment_id,
                           post_id=POST)

    def bot_reply(self, text="hi you too", reply_id="r-1", comment_id=COMMENT):
        return self.insert("ai", "comment", text, comment_id=comment_id, post_id=POST,
                           reply_kind="comment_reply", reply_id=reply_id)

    def row(self, row_id):
        return self.db.execute("SELECT * FROM chat_history WHERE id = ?", (row_id,)).fetchone()

    def history(self):
        return self.client.get(f"/chat/history/{INDEX}/{SESSION}").get_json()


class ReplyActionsTest(InboxCase):
    # --- edit ---

    def test_edit_posts_replacement_then_deletes_original(self):
        self.comment()
        original = self.bot_reply()
        res = self.client.put(f"{BASE}/messages/{original}", json={"text": "  fixed text "})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["status"], "edited")
        self.assertEqual(self.ig.calls, [("reply_comment", COMMENT, "fixed text"),
                                         ("delete_comment", "r-1")])
        new = body["message"]
        self.assertEqual((new["actor"], new["reply_kind"], new["reply_id"], new["replaces_id"],
                          new["comment_id"]),
                         ("agent", "comment_reply", "new-1", original, COMMENT))
        self.assertIsNotNone(body["original"]["deleted_at"])
        self.assertEqual(body["original"]["replaced_by"], new["id"])
        edit = self.db.execute("SELECT * FROM reply_edits").fetchone()
        self.assertEqual((edit["status"], edit["replacement_id"]), ("done", new["id"]))

    def test_original_can_only_be_edited_once_but_replacement_can_be_edited(self):
        self.comment()
        original = self.bot_reply()
        new_id = self.client.put(f"{BASE}/messages/{original}",
                                 json={"text": "v2"}).get_json()["message"]["id"]
        again = self.client.put(f"{BASE}/messages/{original}", json={"text": "v3"})
        self.assertEqual(again.status_code, 409)
        chained = self.client.put(f"{BASE}/messages/{new_id}", json={"text": "v3"})
        self.assertEqual(chained.status_code, 200)
        self.assertEqual(chained.get_json()["message"]["replaces_id"], new_id)

    def test_concurrent_edit_of_same_reply_is_rejected(self):
        self.comment()
        original = self.bot_reply()
        self.db.execute("INSERT INTO reply_edits (index_name, session_id, original_id, text, "
                        "status, created_at, updated_at) VALUES (?, ?, ?, 'x', 'pending', ?, ?)",
                        (INDEX, SESSION, original, ago(), ago()))
        self.db.commit()
        res = self.client.put(f"{BASE}/messages/{original}", json={"text": "v2"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("already being edited", res.get_json()["error"])
        self.assertEqual(self.ig.calls, [])

    def test_edit_whose_post_fails_changes_nothing_and_can_be_retried(self):
        self.comment()
        original = self.bot_reply()
        self.ig.fail.add("reply_comment")
        res = self.client.put(f"{BASE}/messages/{original}", json={"text": "v2"})
        self.assertEqual(res.status_code, 502)
        self.assertIsNone(self.row(original)["deleted_at"])
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.db.execute("SELECT status FROM reply_edits").fetchone()[0], "failed")
        self.ig.fail.clear()
        retry = self.client.put(f"{BASE}/messages/{original}", json={"text": "v2"})
        self.assertEqual(retry.status_code, 200)

    def test_edit_whose_delete_fails_is_partial_and_original_stays_deletable(self):
        self.comment()
        original = self.bot_reply()
        self.ig.fail.add("delete_comment")
        res = self.client.put(f"{BASE}/messages/{original}", json={"text": "v2"})
        self.assertEqual(res.status_code, 207)
        body = res.get_json()
        self.assertEqual(body["status"], "partial")
        self.assertIsNone(body["original"]["deleted_at"])
        self.assertEqual(body["original"]["replaced_by"], body["message"]["id"])
        self.ig.fail.clear()
        deleted = self.client.delete(f"{BASE}/messages/{original}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.db.execute("SELECT status FROM reply_edits").fetchone()[0], "done")

    def test_edit_rejects_non_editable_messages(self):
        comment = self.comment()
        dm = self.insert("ai", "dm", "hello", reply_kind="dm", reply_id="m-1",
                         conversation_id=CONV)
        private = self.insert("ai", "comment", "psst", comment_id=COMMENT, post_id=POST,
                              reply_kind="private_reply", reply_id="m-2")
        unknown_id = self.bot_reply(reply_id=None)
        deleted = self.bot_reply(reply_id="r-9")
        self.db.execute("UPDATE chat_history SET deleted_at = ? WHERE id = ?", (ago(), deleted))
        self.db.commit()
        same = self.bot_reply(text="same", reply_id="r-8")
        cases = [(comment, "x", 400), (dm, "x", 400), (private, "x", 400),
                 (unknown_id, "x", 409), (deleted, "x", 409), (same, "same", 400),
                 (same, "   ", 400), (99999, "x", 404)]
        for message_id, text, status in cases:
            with self.subTest(message_id=message_id, text=text):
                res = self.client.put(f"{BASE}/messages/{message_id}", json={"text": text})
                self.assertEqual(res.status_code, status, res.get_json())
        self.assertEqual(self.ig.calls, [])

    def test_message_of_another_session_is_not_found(self):
        other = self.insert("ai", "comment", "x", session="someone-else", comment_id="c-9",
                            reply_kind="comment_reply", reply_id="r-9")
        self.assertEqual(self.client.put(f"{BASE}/messages/{other}",
                                         json={"text": "y"}).status_code, 404)
        self.assertEqual(self.client.delete(f"{BASE}/messages/{other}").status_code, 404)

    # --- delete ---

    def test_delete_bot_reply_is_idempotent(self):
        self.comment()
        reply = self.bot_reply()
        first = self.client.delete(f"{BASE}/messages/{reply}")
        self.assertEqual(first.get_json()["status"], "deleted")
        second = self.client.delete(f"{BASE}/messages/{reply}")
        self.assertEqual(second.get_json()["status"], "already_deleted")
        self.assertEqual(self.ig.calls, [("delete_comment", "r-1")])

    def test_delete_customer_comment_removes_its_replies_and_blocks_further_replies(self):
        comment = self.comment()
        reply = self.bot_reply()
        private = self.insert("ai", "comment", "psst", comment_id=COMMENT, post_id=POST,
                              reply_kind="private_reply", reply_id="m-2")
        other = self.comment(comment_id="c-200")
        res = self.client.delete(f"{BASE}/messages/{comment}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.ig.calls, [("delete_comment", COMMENT)])
        for row_id in (comment, reply):
            self.assertIsNotNone(self.row(row_id)["deleted_at"])
        for row_id in (private, other):  # a DM stays in the inbox; other comments untouched
            self.assertIsNone(self.row(row_id)["deleted_at"])
        blocked = self.client.post(f"{BASE}/reply-comment", json={"text": "x",
                                                                  "comment_id": COMMENT})
        self.assertEqual(blocked.status_code, 409)

    def test_dms_and_private_replies_cannot_be_deleted(self):
        dm = self.insert("customer", "dm", "hi", conversation_id=CONV)
        private = self.insert("ai", "comment", "psst", comment_id=COMMENT,
                              reply_kind="private_reply", reply_id="m-2")
        for message_id in (dm, private):
            self.assertEqual(self.client.delete(f"{BASE}/messages/{message_id}").status_code, 400)
        self.assertEqual(self.ig.calls, [])

    def test_failed_delete_leaves_message_live(self):
        self.comment()
        reply = self.bot_reply()
        self.ig.fail.add("delete_comment")
        self.assertEqual(self.client.delete(f"{BASE}/messages/{reply}").status_code, 502)
        self.assertIsNone(self.row(reply)["deleted_at"])

    def test_delete_by_instagram_comment_id(self):
        self.comment()
        reply = self.bot_reply()
        res = self.client.delete(f"{BASE}/comments/r-1")
        self.assertEqual(res.status_code, 200)
        self.assertIsNotNone(self.row(reply)["deleted_at"])
        self.assertEqual(self.client.delete(f"{BASE}/comments/nope").status_code, 404)

    # --- private reply ---

    def test_private_reply_once_per_comment(self):
        self.comment()
        res = self.client.post(f"{BASE}/private-reply", json={"text": "check DMs",
                                                              "comment_id": COMMENT})
        self.assertEqual(res.status_code, 200)
        message = res.get_json()["message"]
        self.assertEqual((message["actor"], message["reply_kind"], message["reply_id"]),
                         ("agent", "private_reply", "new-1"))
        claim = self.db.execute("SELECT * FROM auto_replies").fetchone()
        self.assertEqual((claim["mode"], claim["kind"], claim["source_key"], claim["status"]),
                         ("agent", "private_reply", COMMENT, "sent"))
        again = self.client.post(f"{BASE}/private-reply", json={"text": "again",
                                                                "comment_id": COMMENT})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(len(self.ig.calls), 1)

    def test_private_reply_blocked_when_bot_already_sent_one(self):
        self.comment()
        self.insert("ai", "comment", "psst", comment_id=COMMENT, post_id=POST,
                    reply_kind="private_reply", reply_id="m-2")
        res = self.client.post(f"{BASE}/private-reply", json={"text": "x", "comment_id": COMMENT})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.ig.calls, [])

    def test_bot_cannot_send_a_second_private_reply_after_the_agent(self):
        self.comment()
        self.client.post(f"{BASE}/private-reply", json={"text": "x", "comment_id": COMMENT})
        meta = {"instagram_event_type": "comment", "session_id": SESSION,
                "instagram_comment_id": COMMENT, "instagram_post_id": POST,
                "instagram_account_id": ACCOUNT, "user_query": "hi"}
        self.db.execute("INSERT INTO reply_configs (index_name, type, received, reply, "
                        "comment_reply, created_at, updated_at) VALUES "
                        "(?, 'comment', NULL, 'auto dm', 'auto public', ?, ?)",
                        (INDEX, ago(), ago()))
        self.db.commit()
        sent = auto_reply.handle_event(self.db, self.ig, INDEX, meta, "req-1")
        self.assertEqual([r["kind"] for r in sent], ["comment_reply"])
        self.db.execute("DELETE FROM reply_configs")
        self.db.commit()

    def test_private_reply_outside_7_days_is_rejected_and_failed_one_is_retryable(self):
        self.comment(created_at=ago(days=8))
        res = self.client.post(f"{BASE}/private-reply", json={"text": "x", "comment_id": COMMENT})
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.ig.calls, [])
        self.db.execute("UPDATE chat_history SET created_at = ?", (ago(days=1),))
        self.db.commit()
        self.ig.fail.add("private_reply")
        self.assertEqual(self.client.post(f"{BASE}/private-reply",
                                          json={"text": "x", "comment_id": COMMENT}).status_code,
                         502)
        self.ig.fail.clear()
        self.assertEqual(self.client.post(f"{BASE}/private-reply",
                                          json={"text": "x", "comment_id": COMMENT}).status_code,
                         200)

    # --- agent DM / public reply ---

    def test_agent_dm_respects_24h_window(self):
        self.insert("customer", "dm", "hi", created_at=ago(hours=25), conversation_id=CONV)
        self.assertEqual(self.client.post(f"{BASE}/send-message",
                                          json={"text": "late"}).status_code, 422)
        self.insert("customer", "dm", "hi again", conversation_id=CONV)
        res = self.client.post(f"{BASE}/send-message", json={"text": "hello"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["message"]["reply_id"], "new-1")
        self.assertEqual(self.ig.calls, [("send_msg", CONV, "hello")])

    def test_agent_public_reply_stores_reply_id_so_it_can_be_edited(self):
        self.comment()
        res = self.client.post(f"{BASE}/reply-comment", json={"text": "agent here"})
        message = res.get_json()["message"]
        self.assertEqual((message["reply_kind"], message["reply_id"]), ("comment_reply", "new-1"))
        edit = self.client.put(f"{BASE}/messages/{message['id']}", json={"text": "agent v2"})
        self.assertEqual(edit.status_code, 200)

    # --- screen endpoints ---

    def test_history_exposes_action_fields(self):
        self.comment()
        self.bot_reply()
        last = self.history()[-1]
        for key in ("reply_id", "deleted_at", "replaces_id", "replaced_by", "comment_id",
                    "reply_kind"):
            self.assertIn(key, last)

    def test_assets_lists_commented_posts(self):
        self.comment()
        body = self.client.get(f"/api/admin/instagram-assets?index_name={INDEX}").get_json()
        self.assertEqual((body["total_count"], body["items"][0]["asset_id"]), (1, POST))
        self.assertEqual(self.client.get(
            f"/api/admin/instagram-assets/post/{POST}?index_name={INDEX}").status_code, 200)
        self.assertEqual(self.client.get(
            f"/api/admin/instagram-assets/post/nope?index_name={INDEX}").status_code, 404)

    def test_filter_popup_product_names(self):
        res = self.client.get(f"/internal/filter-options/{INDEX}/productNames")
        self.assertEqual(res.get_json(), {"productNames": []})
        preflight = self.client.options(f"/internal/filter-options/{INDEX}/productNames",
                                        headers={"Origin": "http://localhost:3000",
                                                 "Access-Control-Request-Method": "GET",
                                                 "Access-Control-Request-Headers": "auth-version"})
        self.assertEqual(preflight.status_code, 200)


class HoldTest(InboxCase):
    EMAIL = "candidate@example.com"
    OTHER = "other@example.com"

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM reply_configs")
        self.db.execute("DROP TRIGGER IF EXISTS hold_after_claim")
        self.db.commit()

    def rule(self, reply="auto dm", comment_reply="auto public"):
        self.db.execute(
            "INSERT INTO reply_configs (index_name, type, received, reply, comment_reply, "
            "created_at, updated_at) VALUES (?, 'comment', NULL, ?, ?, ?, ?)",
            (INDEX, reply, comment_reply, ago(), ago()))
        self.db.commit()

    def meta(self, comment_id=COMMENT):
        return {"instagram_event_type": "comment", "session_id": SESSION,
                "instagram_comment_id": comment_id, "instagram_post_id": POST,
                "instagram_account_id": ACCOUNT, "customer_username": "cust",
                "user_query": "hi"}

    def test_takeover_transfers_and_anyone_can_release(self):
        self.comment()
        self.assertEqual(self.client.post(f"{BASE}/hold", json={}).status_code, 400)
        self.assertEqual(self.client.post(f"{BASE}/hold",
                                          json={"holder_email": "  "}).status_code, 400)
        taken = self.client.post(f"{BASE}/hold", json={"holder_email": self.EMAIL})
        self.assertEqual(taken.get_json()["holder_email"], self.EMAIL)
        listed = self.client.get(f"/chat/instagram/{INDEX}/sessions").get_json()["sessions"]
        self.assertEqual(listed[0]["holder_email"], self.EMAIL)
        moved = self.client.post(f"{BASE}/hold", json={"holder_email": self.OTHER})
        body = moved.get_json()
        self.assertEqual(body["holder_email"], self.OTHER)
        self.assertEqual(body["created_at"], taken.get_json()["created_at"])
        self.assertEqual(self.client.delete(f"{BASE}/hold").get_json()["status"], "released")
        released = self.client.get(f"/chat/instagram/{INDEX}/sessions").get_json()["sessions"]
        self.assertIsNone(released[0]["holder_email"])
        self.assertEqual(self.client.delete(f"{BASE}/hold").status_code, 200)

    def test_held_session_skips_auto_reply_and_release_does_not_catch_up(self):
        self.comment()
        self.rule()
        self.client.post(f"{BASE}/hold", json={"holder_email": self.EMAIL})
        # The reply worker has its own connection; the hold was committed on the request one.
        sent = auto_reply.handle_event(app_module.auto_reply_db, self.ig, INDEX,
                                        self.meta(), "req-1")
        self.assertEqual(self.ig.calls, [])
        self.assertEqual(sorted(r["kind"] for r in sent), ["comment_reply", "private_reply"])
        self.assertTrue(all(r["status"] == "skipped" and r["error"] == "held" for r in sent))
        self.client.delete(f"{BASE}/hold")
        again = auto_reply.handle_event(app_module.auto_reply_db, self.ig, INDEX,
                                         self.meta(), "req-2")
        self.assertEqual(again, [])
        self.assertEqual(self.ig.calls, [])
        self.comment(comment_id="c-200")
        fresh = auto_reply.handle_event(app_module.auto_reply_db, self.ig, INDEX,
                                         self.meta("c-200"), "req-3")
        self.assertEqual(sorted(r["status"] for r in fresh), ["sent", "sent"])
        self.assertEqual(len(self.ig.calls), 2)

    def test_hold_landing_after_the_claim_still_suppresses_the_send(self):
        self.comment()
        self.rule(reply=None, comment_reply="auto public")
        self.db.execute(
            "CREATE TEMP TRIGGER hold_after_claim AFTER INSERT ON auto_replies BEGIN "
            "INSERT INTO session_holds (index_name, session_id, holder_email, created_at, "
            "updated_at) VALUES (NEW.index_name, NEW.session_id, 'racer@example.com', "
            "NEW.created_at, NEW.updated_at); END")
        try:
            sent = auto_reply.handle_event(self.db, self.ig, INDEX, self.meta(), "req-race")
        finally:
            self.db.execute("DROP TRIGGER IF EXISTS hold_after_claim")
            self.db.commit()
        self.assertEqual(self.ig.calls, [])
        self.assertEqual([r["error"] for r in sent], ["held"])

    def test_agent_can_still_reply_while_the_session_is_held(self):
        self.insert("customer", "dm", "hi", conversation_id=CONV)
        self.client.post(f"{BASE}/hold", json={"holder_email": self.EMAIL})
        res = self.client.post(f"{BASE}/send-message", json={"text": "I'm here"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.ig.calls, [("send_msg", CONV, "I'm here")])


class MessageEditTest(InboxCase):
    MID = "ig-mid-1"

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM reply_configs")
        self.db.commit()

    def post_event(self, text, is_edit=False, request_id="req-1", message_id=MID, **meta):
        metadata = {"index_name": INDEX, "session_id": SESSION, "user_query": text,
                    "instagram_event_type": "dm", "instagram_conversation_id": CONV,
                    "instagram_account_id": ACCOUNT, "instagram_message_id": message_id,
                    "customer_username": "cust", "is_edit": is_edit, **meta}
        return self.client.post("/events", json={"event_metadata": {
            "index_name": INDEX, "requestId": request_id,
            "original_payload": {"metadata": metadata}}})

    def edit(self, text, edited_at, request_id=None, **meta):
        return self.post_event(text, is_edit=True, request_id=request_id or f"edit-{text}",
                               edited_at=edited_at, **meta)

    def dm(self, text="size S please", message_id=MID):
        return self.insert("customer", "dm", text, created_at="2026-10-01T09:00:00+00:00",
                           conversation_id=CONV, message_id=message_id)

    def test_received_dm_stores_instagram_message_id(self):
        self.assertEqual(self.post_event("hi").get_json()["status"], "stored")
        app_module.auto_reply_worker.submit(lambda: None).result()
        row = self.db.execute("SELECT message_id, edited_at FROM chat_history").fetchone()
        self.assertEqual((row["message_id"], row["edited_at"]), (self.MID, None))

    def test_edit_updates_message_in_place_and_keeps_earlier_versions(self):
        row_id = self.dm()
        self.assertEqual(self.edit("size L please", "2026-10-01T10:00:00Z").get_json()["status"],
                         "edited")
        self.assertEqual(self.edit("size M please", "2026-10-01T11:00:00Z").get_json()["status"],
                         "edited")
        history = self.history()
        self.assertEqual(len(history), 1)
        message = history[0]
        self.assertEqual((message["id"], message["text"], message["message_id"]),
                         (row_id, "size M please", self.MID))
        self.assertEqual(message["edited_at"], "2026-10-01T11:00:00.000000+00:00")
        self.assertEqual(message["edit_history"], [
            {"text": "size S please", "created_at": "2026-10-01T09:00:00+00:00"},
            {"text": "size L please", "created_at": "2026-10-01T10:00:00.000000+00:00"},
        ])
        statuses = [r[0] for r in self.db.execute("SELECT status FROM webhook_events")]
        self.assertEqual(statuses, ["edited", "edited"])

    def test_edit_is_never_auto_replied(self):
        self.dm()
        self.db.execute("INSERT INTO reply_configs (index_name, type, received, reply, "
                        "created_at, updated_at) VALUES (?, 'dm', NULL, 'auto dm', ?, ?)",
                        (INDEX, ago(), ago()))
        self.db.commit()
        self.edit("size L please", "2026-10-01T10:00:00Z")
        app_module.auto_reply_worker.submit(lambda: None).result()
        self.assertEqual(self.ig.calls, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM auto_replies").fetchone()[0], 0)

    def test_redelivered_and_out_of_order_edits_change_nothing(self):
        self.dm()
        self.edit("size M please", "2026-10-01T11:00:00Z")
        again = self.edit("size M please", "2026-10-01T11:00:00Z", request_id="edit-again")
        self.assertEqual(again.get_json()["status"], "duplicate")
        older = self.edit("size L please", "2026-10-01T10:00:00Z")
        self.assertEqual(older.get_json()["status"], "stale")
        message = self.history()[0]
        self.assertEqual((message["text"], len(message["edit_history"])), ("size M please", 1))

    def test_edit_of_unknown_message_is_ignored(self):
        res = self.edit("size L please", "2026-10-01T10:00:00Z")
        self.assertEqual((res.status_code, res.get_json()["status"]), (200, "ignored"))
        self.assertEqual(self.history(), [])

    def test_edit_of_row_stored_before_message_id_matches_by_previous_text(self):
        row_id = self.dm(message_id=None)
        self.dm(text="something else", message_id=None)
        res = self.edit("size L please", "2026-10-01T10:00:00Z", previous_text="size S please")
        self.assertEqual(res.get_json()["status"], "edited")
        self.assertEqual((self.row(row_id)["text"], self.row(row_id)["message_id"]),
                         ("size L please", self.MID))

    def test_unedited_messages_have_empty_edit_history(self):
        self.comment()
        self.dm()
        self.assertTrue(all(m["edit_history"] == [] for m in self.history()))


class DuplicateDeliveryTest(InboxCase):
    """Zernio can deliver one Instagram message as two events, each forwarded with
    its own requestId; the message is stored and auto-replied once."""
    MID = "ig-mid-1"

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM reply_configs")
        for reply_type, reply, comment_reply in (("dm", "auto dm", None),
                                                 ("comment", None, "auto public")):
            self.db.execute("INSERT INTO reply_configs (index_name, type, received, reply, "
                            "comment_reply, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?, ?)",
                            (INDEX, reply_type, reply, comment_reply, ago(), ago()))
        self.db.commit()

    def tearDown(self):
        self.db.execute("DELETE FROM reply_configs")
        self.db.commit()

    def deliver(self, request_id, text="hi", event_type="dm", message_id=MID, comment_id="",
                zernio_event_id="evt-1"):
        metadata = {"index_name": INDEX, "session_id": SESSION, "user_query": text,
                    "instagram_event_type": event_type, "instagram_conversation_id": CONV,
                    "instagram_account_id": ACCOUNT, "instagram_message_id": message_id,
                    "instagram_comment_id": comment_id, "instagram_post_id": POST,
                    "customer_username": "cust", "zernio_event_id": zernio_event_id}
        res = self.client.post("/events", json={"event_metadata": {
            "index_name": INDEX, "requestId": request_id,
            "original_payload": {"metadata": metadata}}})
        app_module.auto_reply_worker.submit(lambda: None).result()
        return res.get_json()["status"]

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_duplicate_dm_is_stored_and_auto_replied_once(self):
        self.assertEqual(self.deliver("req-1", zernio_event_id="evt-1"), "stored")
        self.assertEqual(self.deliver("req-2", zernio_event_id="evt-2"), "duplicate")
        self.assertEqual(self.count("chat_history WHERE actor = 'customer'"), 1)
        self.assertEqual(self.ig.calls, [("send_msg", CONV, "auto dm")])
        events = self.db.execute(
            "SELECT status, zernio_event_id FROM webhook_events ORDER BY id").fetchall()
        self.assertEqual([tuple(e) for e in events], [("stored", "evt-1"), ("duplicate", "evt-2")])

    def test_duplicate_comment_is_stored_and_auto_replied_once(self):
        for request_id in ("req-1", "req-2"):
            self.deliver(request_id, event_type="comment", message_id="", comment_id=COMMENT)
        self.assertEqual(self.count("chat_history WHERE actor = 'customer'"), 1)
        self.assertEqual(self.ig.calls, [("reply_comment", COMMENT, "auto public")])

    def test_same_text_sent_twice_is_two_messages(self):
        self.assertEqual(self.deliver("req-1", message_id="ig-mid-1"), "stored")
        self.assertEqual(self.deliver("req-2", message_id="ig-mid-2"), "stored")
        self.assertEqual(self.count("chat_history WHERE actor = 'customer'"), 2)
        self.assertEqual(len(self.ig.calls), 2)

    def test_dms_without_message_id_are_each_stored(self):
        self.assertEqual(self.deliver("req-1", message_id=""), "stored")
        self.assertEqual(self.deliver("req-2", message_id=""), "stored")
        self.assertEqual(self.count("chat_history WHERE actor = 'customer'"), 2)


class OneRowPerMessageMigrationTest(unittest.TestCase):
    def test_duplicates_stored_before_the_index_are_folded_into_the_first_row(self):
        path = os.path.join(_tmp.name, "legacy.db")
        conn = database.connect(path)
        for index, _, _ in database.CUSTOMER_MESSAGE_KEYS:
            conn.execute(f"DROP INDEX {index}")
        rows = [("customer", "dm", "m-1", ""), ("customer", "dm", "m-1", ""),
                ("customer", "comment", None, "c-1"), ("customer", "comment", None, "c-1"),
                ("ai", "comment", None, "c-1"), ("customer", "dm", "m-2", "")]
        ids = [conn.execute("INSERT INTO chat_history (index_name, session_id, actor, event_type, "
                            "message_id, comment_id, text) VALUES (?, ?, ?, ?, ?, ?, 'hi')",
                            (INDEX, SESSION, *r)).lastrowid for r in rows]
        conn.execute("INSERT INTO message_edits (message_id, history_id, edited_at, created_at) "
                     "VALUES ('m-1', ?, 'x', 'x')", (ids[1],))
        conn.commit()
        conn.close()

        conn = database.connect(path)
        remaining = [r[0] for r in conn.execute("SELECT id FROM chat_history ORDER BY id")]
        self.assertEqual(remaining, [ids[0], ids[2], ids[4], ids[5]])
        self.assertEqual(conn.execute("SELECT history_id FROM message_edits").fetchone()[0], ids[0])
        conn.close()


if __name__ == "__main__":
    unittest.main()
