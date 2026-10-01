# Submission notes

## 4. Test cases and edge cases tested

Automated suite (`python -m unittest discover -s tests -v`) — **37 tests**, all passing against a fake gateway and temp SQLite DB.

### Auto-replies & duplicates
- Duplicate DM / comment webhook → stored once, auto-replied once (keyed by Instagram message / comment id, not `requestId`)
- Same text sent twice as two real messages → two rows, two replies
- DM without `instagram_message_id` → each delivery stored (cannot dedupe; logged)
- Legacy duplicate rows folded into one on DB migrate

### Platform windows & private replies
- One private reply per comment (bot and agent share the same claim lock)
- Agent blocked after bot already sent; bot blocked after agent already sent
- Outside 7-day private-reply window → rejected / skipped
- Failed private reply is retryable; successful send is not
- Agent DM outside 24h window → 422

### Edit / delete (Task 2)
- Edit = post replacement, then delete original
- One edit claim per original; replacement can be edited again; concurrent edit → 409
- Post failure → nothing changed, retryable; delete failure → `partial` (207), original still deletable
- Rejects edit of customer comment, DM, private reply, deleted/blank/other-session rows
- Idempotent delete; delete by Instagram comment id
- Deleting customer comment cascades to its public replies, blocks further replies; DMs untouched
- DMs / private replies cannot be deleted; failed delete leaves message live
- Agent public replies store `reply_id` so they remain editable

### Agent hold (Task 3)
- Takeover creates / transfers hold; anyone can release
- Held session skips auto-reply; release does **not** catch up on messages that arrived during the hold
- Mid-takeover race: hold landing after the claim still suppresses the Instagram send (pre-send re-check)
- Agent can still DM / reply / private-reply while the session is held

### Customer DM edits
- `message.edited` updates in place, keeps earlier versions, never auto-replies
- Redelivered / out-of-order edits are no-ops; unknown message → ignored
- Rows stored before `message_id` matched by previous text and backfilled

### Inbox contracts
- History exposes `reply_kind`, `reply_id`, `deleted_at`, edit chain fields
- Instagram assets list + empty `productNames` stub (endpoints the chat screen calls)

### Manual / browser (against local gateway)
- Both comment modes (`comment_only`, `comment_and_dm`) and DM auto-reply in the inbox
- Edit, delete bot reply, delete user comment, private reply (then disabled), agent DM
- Partial-edit UI (“still on Instagram”)
- Takeover / release + Held filter; bot stays quiet while held

---

## 5. Short notes

### What I cut
- No product catalogue / rich Instagram asset media (posts listed from comment history only; captions/media not fetched)
- No WebSocket live push — inbox polls (sessions ~15s, messages ~5s)
- No multi-agent assignment / queue — hold is takeover-by-email with transfer
- No retry worker for failed auto-replies (failed bot claims stay claimed; agent private-reply path can retry)
- No unsend for DMs / private replies (Instagram has no API; UI doesn’t offer it)
- Gateway async-offload of webhook enrichment (known 5s Zernio timeout risk) left for later

### Bugs found in the provided code
- Gateway maps every Zernio HTTP error to a bare **500**, so “comment already deleted” (404) is indistinguishable from a real failure
- Webhook dedup on `requestId` missed duplicates (gateway mints a new id per forward; Zernio can also emit two events for one IG message) — fixed with Instagram message/comment id uniqueness (+ gateway-side dedup)
- Gateway wrote Redis dedup keys **before** processing; a failed forward made Zernio’s retry look like a duplicate and the message was lost — fixed by releasing keys on failure
- Gateway enriches + forwards before answering Zernio → can exceed the **5s** limit and cause retries (not fixed; needs off-request processing)
- Chat screen called `/api/admin/instagram-assets` and `/internal/filter-options/.../productNames` with nothing serving them (404 + CORS on custom `auth-version` header) — stubbed in this backend

### What I’d do next with more time
- Background retry for `failed` auto-replies (and reclaim of stuck `pending`)
- Move gateway webhook work off the request path so Zernio gets a fast 200
- Propagate real Zernio status codes through `/actions/*` instead of collapsing to 500
- Fetch post media/captions for the assets panel; optional WS for live inbox
- Stronger audit of holds (who held when, history of transfers) and clearer multi-tab hold UX
- End-to-end tests against a recorded Zernio fixture pack for both comment modes + private-reply windows
