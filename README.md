# Bot Backend — Task 1 (Auto-replies) + Task 2 (Reply actions)

Instagram auto-reply receiver that sits between `channel-integration` (Zernio gateway) and `site-frontend`.

```
Instagram → Zernio → channel-integration → POST /events → bot-backend
                                                              │
                         site-frontend ← HTTP ←───────────────┘
                                                              │
                         channel-integration ← POST /actions/* ┘
```

## Setup

```bash
cd bot-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Point at the local gateway
export INSTAGRAM_GATEWAY_URL=http://localhost:8082
export INTERNAL_API_KEY=dev-internal-key   # must match channel-integration
# optional: export INSTAGRAM_ACCOUNT_ID=<zernio-account-id>

python app.py   # listens on :9000
```

Point the gateway’s outbound webhook / events destination at `http://localhost:9000/events`.
Use `demo-store` as `index_name` everywhere (matches the frontend).

### Env vars

| Variable | Default | Purpose |
|---|---|---|
| `INSTAGRAM_GATEWAY_URL` | `http://localhost:8082` | channel-integration base URL |
| `INTERNAL_API_KEY` | `dev-internal-key` | Shared secret for `/actions/*` |
| `INSTAGRAM_ACCOUNT_ID` | _(none)_ | Fallback Zernio account id |
| `APP_DB_PATH` | `./app.db` | SQLite path |

Logs (JSON lines) go to `logs/bot-backend.log`.

---

## What Task 1 does

1. **Ingest** every inbound DM / comment from the gateway (`POST /events`).
2. **Match** a `reply_configs` rule (exact text, else default for that type).
3. **Send** the configured outbound action(s) through the gateway:
   - DM → one private message (`kind=dm`)
   - Comment → public reply only (`comment_only`) **or** public reply + private reply (`comment_and_dm`)
4. **Persist** every attempt in `auto_replies` and every successful send in `chat_history` so the inbox can show it.
5. **Expose** chat + config APIs that `site-frontend` already calls.

---

## Data model

### `chat_history` — one row per message shown in the inbox

| Column | Notes |
|---|---|
| `request_id` | Unique for inbound webhook deliveries (dedupe) |
| `actor` | `customer` \| `ai` (auto-reply) \| `agent` (manual) |
| `event_type` | `dm` \| `comment` (copied from the inbound event) |
| `reply_kind` | Outbound only: `dm` \| `comment_reply` \| `private_reply` |
| `comment_id` / `conversation_id` / `post_id` / `account_id` | Instagram targets |

`reply_kind` is what lets the UI tell a public comment reply apart from a private reply when both are sent for the same comment (`comment_and_dm`).

### `reply_configs` — auto-reply rules per `index_name`

| Field | DM rule | Comment rule |
|---|---|---|
| `type` | `dm` | `comment` |
| `received` | Match text, or `NULL` = default | same |
| `reply` | Text of the outbound DM | Private reply (DM) to the commenter; `NULL` = no DM |
| `comment_reply` | unused (`NULL`) | Public reply under the comment |

Mode derivation for comments: `reply` set → `comment_and_dm`; else → `comment_only`.

### `auto_replies` — one row per send attempt

| Field | Notes |
|---|---|
| `mode` | `dm` \| `comment_only` \| `comment_and_dm` |
| `kind` | `dm` \| `comment_reply` \| `private_reply` |
| `source_key` | Incoming DM message id or comment id |
| `status` | `pending` → `sent` \| `failed` \| `skipped` |
| Unique | `(kind, source_key)` — claim lock against duplicate webhooks |

### Task 2 additions

`chat_history` gains (migrated in place on connect, backfilled from `auto_replies`):

| Column | Notes |
|---|---|
| `reply_id` | Instagram id of what was sent — the public reply's comment id (needed to delete / replace it), the DM's message id |
| `deleted_at` | Set when the comment was deleted from Instagram via the screen |
| `replaces_id` | On an edit's replacement: the row it replaced. History also returns the inverse, `replaced_by` |

`reply_edits` — one row per edited reply:

| Field | Notes |
|---|---|
| `original_id` | UNIQUE — the claim; an edited reply can't be edited again (edit the replacement instead) |
| `replacement_id` | The new reply's row |
| `status` | `pending` → `done` \| `partial` (replacement live, original delete failed) \| `failed` (nothing posted; retryable) |

`auto_replies.mode` also takes `agent` for a private reply sent from the screen (see below).

### Customer DM edits (`message.edited`)

`chat_history` gains `message_id` (Instagram's id for a customer DM) and `edited_at`.
`message_edits` keeps one row per edit — `message_id`, `history_id` (the row it was
applied to), `previous_text`, `text`, `edited_at` — oldest first.

### `webhook_events` — audit of every `POST /events` delivery

Status: `stored` \| `duplicate` \| `ignored`, and for edits `edited` \| `duplicate` \| `stale` \| `ignored`.
`zernio_event_id` is Zernio's id for the webhook event: a retry keeps it, a second event
for the same Instagram message does not.

### Duplicate deliveries

A message is identified by Instagram's id for it — `instagram_message_id` for a DM,
`instagram_comment_id` for a comment — never by `requestId` (new on every forward) or by
text and time (a customer can send "hi" twice). `chat_history` holds one customer row per
message: unique indexes on `(index_name, message_id)` and `(index_name, comment_id)` make a
second delivery a no-op, recorded in `webhook_events` as `duplicate`, and it is never
auto-replied. A DM without a message id can't be checked; it is stored and a warning is
logged. Duplicates stored before these indexes are folded into the first row on connect.

---

## API (Task 1 surface)

### Ingest

- `POST /events` — gateway webhook. Stores customer row, then runs auto-reply off-thread (gateway 10s timeout).

### Inbox (used by site-frontend)

- `GET /chat/instagram/<index>/channel-check`
- `GET /chat/instagram/<index>/sessions?startDate=&endDate=`
- `GET /chat/history/<index>/<session_id>` → includes `event_type`, `reply_kind`, `comment_id`
- `GET /chat/instagram/<index>/<session_id>/context`

### Manual outbound + reply actions (Task 2)

All under `/chat/instagram/<index>/<session_id>`; sent replies are stored as `actor=agent`.

| Endpoint | Body | Notes |
|---|---|---|
| `POST .../send-message` | `{text}` | DM; **422** outside the 24h window |
| `POST .../reply-comment` | `{text, comment_id?}` | Public reply; **409** if the comment was deleted |
| `POST .../private-reply` | `{text, comment_id?}` | **409** if one was already sent (by bot or agent), **422** after 7 days |
| `PUT .../messages/<id>` | `{text}` | Edit a public comment reply — see *Edit* below. `200 edited` / `207 partial` |
| `DELETE .../messages/<id>` | — | Delete a public comment reply or the customer's comment. Idempotent |
| `DELETE .../comments/<instagram id>` | — | Same delete, addressed by Instagram comment id |
| `POST .../comments/<id>/hide\|unhide` | — | Pass-through |

`<id>` is the `chat_history` id the screen already has. Errors are `{"error": "..."}`:
400 bad request / not an editable message, 404 not in this conversation, 409 state
conflict, 422 Instagram window closed, 502 gateway failure (with `details`).

### Other endpoints the chat screen calls

- `GET /api/admin/instagram-assets?index_name=` and `/<type>/<id>` — posts seen in comments (no media/caption: this backend doesn't fetch post metadata)
- `GET /internal/filter-options/<index>/productNames` — `{"productNames": []}` (no catalogue)

### Reply config CRUD

- `GET|POST /chat/instagram/<index>/reply-configs`
- `PUT|DELETE /chat/instagram/<index>/reply-configs/<id>`
- `GET /chat/instagram/<index>/auto-replies?session_id=&status=`

---

## Key decisions

### Duplicates

Webhooks can arrive twice with different `requestId`s. Dedup is two-layer:

1. `chat_history.request_id UNIQUE` — second delivery of the same envelope is ignored for ingest.
2. `auto_replies (kind, source_key) UNIQUE` — only the claimer of a `(kind, source_key)` may call the gateway. A retry of the same comment/DM cannot double-send.

### Platform windows

- **DM**: only within 24h of the customer’s last message in that conversation → otherwise `skipped`.
- **Private reply**: at most one per comment, and only within 7 days of the customer comment → otherwise `skipped`. Meta silently drops extras; we never attempt a second one.

### Persistence of outbound replies

Every successful auto-reply writes:

1. `auto_replies` row (`status=sent`, `reply_id` when the gateway returns one, plus mode/kind/parent ids).
2. `chat_history` row with `actor=ai` and `reply_kind` set so the chat screen can render it.

### Async reply path

Auto-replies run on a single-worker thread pool with their own DB connection so the webhook can return quickly and sends stay serialized.

### History fields for the UI

`GET /chat/history` returns `event_type`, `reply_kind`, and `comment_id` so the frontend can:

- Label inbound as DM vs Comment
- Group a public reply + private reply under the parent comment
- Distinguish outbound kinds without joining `auto_replies`

Existing AI rows are backfilled from `auto_replies` on DB connect when `reply_kind` is missing.

### Edit (Instagram can't edit a published comment)

An edit is a **replacement**: post the new text as a reply under the same parent
comment, then delete the original.

1. **Claim** the original in `reply_edits` (`UNIQUE original_id`) — a double-click or second tab gets 409, not two replacements.
2. **Post first, delete second.** If the post fails, nothing changed (`failed`, retryable). If only the delete fails, both are live (`partial`, HTTP 207): the screen flags the old version "still on Instagram" and keeps *Delete* on it; deleting it later completes the edit.
3. The screen shows one reply with an **Edited** badge and the earlier versions under *Edit history*.

Trade-offs, accepted: the replacement is a new comment — new id, new timestamp, sorts
last in the thread on Instagram, loses likes on the original, and may notify the
customer again. Delete-then-post was rejected because a failed post would leave the
customer with no answer.

### Customer edits a DM

Zernio sends `message.edited` with the latest text; the gateway forwards it to `POST /events`
with `is_edit: true`. It is not a new message — no row is inserted and no auto-reply runs.

1. **Match** the stored message by `message_id`. Rows stored before that column have none,
   so the customer's latest DM in the conversation whose text equals the edit's
   `previous_text` is used instead, and its `message_id` is backfilled.
2. **Update in place**: `text` becomes the latest version, `edited_at` is set, and the
   replaced text goes to `message_edits`.
3. **Idempotent**: same text again → `duplicate`; older than the last applied edit →
   `stale`; no stored message → `ignored` (HTTP 200, so Zernio doesn't retry).

`GET /chat/history` returns `message_id`, `edited_at` and `edit_history` (earlier versions,
oldest first, each `{text, created_at}` — when that version was written). The screen shows
the DM with an **Edited** badge and the earlier versions under *Edit history*, like an
edited comment reply.

### Delete

Only comments can be deleted — there's no API to unsend a DM or a private reply, so the
screen doesn't offer it. Deleting the **customer's comment** also marks the replies under
it deleted, since Instagram removes a comment's replies
with it. Deleted rows stay in history (struck through) rather than disappearing.
A second delete returns `already_deleted` without calling the gateway.

### Private reply from the screen

Instagram allows one per comment, within 7 days, and silently drops extras. The agent
send claims the **same** `auto_replies (private_reply, comment_id)` lock as the bot, so a
bot and an agent can never both send one (a failed/skipped claim may be retried). It's
also refused when `chat_history` already has a private reply for the comment.

### Hold (takeover / release)

A **hold** is one row in `session_holds`, keyed by `(index_name, session_id)` — the same
session that groups a customer's DMs and comments. `POST .../hold` with `{holder_email}`
creates it, or transfers it when someone already holds the session. `DELETE .../hold`
releases it; anyone can release. The session list includes `holder_email` (null when the
bot is handling it). The inbox's **Held** filter is that list, narrowed to rows with a holder.

While a hold exists, `auto_reply` still stores the customer message, but each planned
send is claimed and then marked `skipped` / `error=held` after a fresh read of the hold
and before the Instagram call. Release does not answer those messages. The next customer
message, after release, is answered normally. An agent can still DM, reply, and private-reply
during a hold — those routes do not check it.

The reply runs on a background worker, so takeover can commit after the event was queued.
The worker's pre-send read closes that gap. A call that has already been handed to
Instagram is not cancelled.

The composer's DM-vs-comment target stays on the screen. *Agent handoff* takes the
session over and opens the composer; *Hand back to bot* releases it.

---

## Frontend changes (Task 2 UI)

In `site-frontend` (`/admin/chats` → Instagram screen):

| Change | Where |
|---|---|
| ⋮ menu on every bot/agent public reply: **Edit**, **Delete reply**, **Send private reply in DM** (disabled once sent / after 7 days), **Agent handoff**, **Delete user's comment** | `channels/instagram.tsx` (`CommentThreadCard`) |
| Same comment-level actions on the customer's comment | same |
| Inline edit with the replace-not-edit note; *Edited* badge + edit history; *Deleted* / *Old version · still on Instagram* states | same |
| A DM the customer edited: *Edited* badge + edit history (shared `EditHistory`) | `channels/instagram.tsx` (`DmBubble`) |
| Agent composer under the conversation (DM / public reply target, 24h window notice) | `components/ReplyActions.tsx` |
| Conversation refreshes every 5s (sessions every 15s) and after each action, silently | `hooks/` |
| Duplicate deliveries of one comment render as one card | `channels/instagram.tsx` |

---

## Frontend changes (Task 1 UI)

In `site-frontend` (`/admin/chats/instagram`):

| Change | Why |
|---|---|
| Reply-config modal | Configure DM / comment / comment+DM rules without curling the API |
| History adapter maps `event_type`, `reply_kind`, `comment_id` | Drive per-message UI |
| Comment **thread cards** | Comments render as a public post thread (customer comment → nested public reply → private-reply footer), not identical chat bubbles |
| DM bubbles | Stay messenger-style under a “Direct messages · private inbox” divider |
| Activity filter (All / DMs / Comments) | Scan mixed sessions quickly |
| Session list lane icons | Show last event type without loud colour fills |

Palette stays neutral (slate / white); indigo = DM icon accent, amber = comment icon accent.

---

## Files

| File | Role |
|---|---|
| `app.py` | Flask routes, ingest, chat APIs, config CRUD |
| `auto_reply.py` | Rule match, plan, claim, send, record |
| `reply_actions.py` | Task 2: edit (replace), delete, private-reply lock, window checks |
| `database.py` | Schema + migrations (`reply_kind`, legacy reply_configs) |
| `instagram_service.py` | Gateway `/actions/*` client |
| `logging_service.py` | JSON request/response/webhook logs |

---

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_reply_actions.py` (fake gateway, temp DB) covers: edit = post-then-delete;
one edit per original, re-editing the replacement; concurrent edit rejected; post
failure changes nothing and is retryable; delete failure → `partial`, original still
deletable; non-editable messages (customer comment, DM, private reply, unknown id,
deleted, unchanged/blank text, other session); idempotent delete; deleting the
customer's comment cascades to its replies but not DMs, and blocks
further replies; DMs/private replies can't be deleted; failed delete leaves the message
live; delete by Instagram id; private reply once per comment, blocked after the bot's,
bot blocked after the agent's, 7-day window, retry after failure; agent DM 24h window;
agent replies store `reply_id` so they're editable; history / assets / productNames.

Also verified in the browser against a fake gateway: edit, delete reply, delete
customer comment, private reply (then disabled), agent DM, partial-edit display.

## Bugs / gaps found in the provided code

- The gateway re-raises every Zernio HTTP error as a bare 500 (`zernio/client.py` → no handler in `actions/`), so the backend can't tell "comment already deleted" (404) from a real failure.
- Webhook dedup on `requestId` didn't catch duplicates (the gateway mints a new id per forward), and Zernio can emit two events for one Instagram message. Fixed: one `chat_history` row per Instagram message id (see *Duplicate deliveries*), and the gateway also dedups per message id.
- The gateway set its Redis dedup key before processing, so when forwarding failed Zernio's retry was skipped as a duplicate and the message was lost. Fixed: the keys are released on failure.
- The gateway enriches and forwards before answering Zernio, which can exceed Zernio's 5-second limit and trigger retries. Not fixed: needs processing moved off the request.
- The chat screen calls `/api/admin/instagram-assets` and `/internal/filter-options/.../productNames`, which nothing served (404 + CORS failure on the custom `auth-version` header).

## Manual test checklist (Task 1)

- [ ] Inbound DM → static DM auto-reply appears in inbox (`reply_kind=dm`)
- [ ] Comment + `comment_only` → one public reply under the comment
- [ ] Comment + `comment_and_dm` → public reply **and** private reply; both visible on the same comment card
- [ ] Duplicate webhook → no second send (`auto_replies` claim / `request_id`)
- [ ] Outside 24h DM window → `skipped`, no chat row for that kind
- [ ] Second private reply on same comment → not attempted (`skipped` / unique claim)
- [ ] Reply-config modal create/update/delete reflects in next inbound event
