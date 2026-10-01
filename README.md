# Bot Backend — Task 1 (Auto-replies)

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

### `webhook_events` — audit of every `POST /events` delivery

Status: `stored` \| `duplicate` \| `ignored`.

---

## API (Task 1 surface)

### Ingest

- `POST /events` — gateway webhook. Stores customer row, then runs auto-reply off-thread (gateway 10s timeout).

### Inbox (used by site-frontend)

- `GET /chat/instagram/<index>/channel-check`
- `GET /chat/instagram/<index>/sessions?startDate=&endDate=`
- `GET /chat/history/<index>/<session_id>` → includes `event_type`, `reply_kind`, `comment_id`
- `GET /chat/instagram/<index>/<session_id>/context`

### Manual outbound (prep for Task 2; already wired)

- `POST .../send-message` `{text}` → DM (`reply_kind=dm`)
- `POST .../reply-comment` `{text, comment_id?}` → public reply
- `POST .../private-reply` `{text, comment_id?}` → private reply to commenter
- Hide / unhide / delete comment helpers

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
| `database.py` | Schema + migrations (`reply_kind`, legacy reply_configs) |
| `instagram_service.py` | Gateway `/actions/*` client |
| `logging_service.py` | JSON request/response/webhook logs |

---

## Manual test checklist (Task 1)

- [ ] Inbound DM → static DM auto-reply appears in inbox (`reply_kind=dm`)
- [ ] Comment + `comment_only` → one public reply under the comment
- [ ] Comment + `comment_and_dm` → public reply **and** private reply; both visible on the same comment card
- [ ] Duplicate webhook → no second send (`auto_replies` claim / `request_id`)
- [ ] Outside 24h DM window → `skipped`, no chat row for that kind
- [ ] Second private reply on same comment → not attempted (`skipped` / unique claim)
- [ ] Reply-config modal create/update/delete reflects in next inbound event
