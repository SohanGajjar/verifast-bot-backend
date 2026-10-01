"""Outbound Instagram actions via the channel-integration gateway's /actions/* API.

    ig = InstagramService(base_url="http://localhost:8082",
                          internal_key="dev-internal-key",
                          account_id="<zernio account id>")
    ig.reply_comment(meta["instagram_comment_id"], "Thanks!")
    ig.send_msg(meta["instagram_conversation_id"], "Hi there")

`account_id` set in the constructor is the default; every method also accepts
an explicit `account_id` (e.g. the event's `instagram_account_id`).
"""
import logging
import time
from typing import Any, Dict, Optional

import requests

from logging_service import body_for_log

log = logging.getLogger("bot_backend.instagram")


class InstagramServiceError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class InstagramService:
    def __init__(self, base_url: str, internal_key: str, account_id: Optional[str] = None,
                 timeout: float = 10, session: Optional[requests.Session] = None):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.update({
            "X-Internal-Key": internal_key or "",
            "Content-Type": "application/json",
        })

    # --- DMs ---

    def send_msg(self, conversation_id: str, text: str, account_id: Optional[str] = None) -> Dict:
        """DM into an existing conversation (Meta's 24h window applies)."""
        return self._request("POST", "/actions/send-dm", json={
            "conversation_id": conversation_id,
            "text": text,
            "account_id": self._account(account_id),
        })

    # --- Comments ---

    def reply_comment(self, comment_id: str, text: str, account_id: Optional[str] = None) -> Dict:
        """Public reply under a comment."""
        return self._request("POST", "/actions/reply-comment", json={
            "comment_id": comment_id,
            "text": text,
            "account_id": self._account(account_id),
        })

    def private_reply(self, post_id: str, comment_id: str, text: str,
                      account_id: Optional[str] = None) -> Dict:
        """Private DM to a commenter (Meta allows one per comment per 7 days)."""
        return self._request("POST", "/actions/private-reply", json={
            "post_id": post_id,
            "comment_id": comment_id,
            "text": text,
            "account_id": self._account(account_id),
        })

    def hide_comment(self, comment_id: str, account_id: Optional[str] = None) -> Dict:
        return self._request("POST", "/actions/hide-comment", json={
            "comment_id": comment_id,
            "account_id": self._account(account_id),
        })

    def unhide_comment(self, comment_id: str, account_id: Optional[str] = None) -> Dict:
        return self._request("POST", "/actions/unhide-comment", json={
            "comment_id": comment_id,
            "account_id": self._account(account_id),
        })

    def delete_comment(self, comment_id: str, account_id: Optional[str] = None) -> Dict:
        return self._request("DELETE", f"/actions/comment/{comment_id}",
                             params={"account_id": self._account(account_id)})

    # --- internals ---

    def _account(self, account_id: Optional[str]) -> str:
        resolved = account_id or self.account_id
        if not resolved:
            raise ValueError("account_id is required (pass it or set it in the constructor)")
        return resolved

    def _request(self, method: str, path: str, **kwargs) -> Dict:
        kwargs.setdefault("timeout", self.timeout)
        url = f"{self.base_url}{path}"
        log.info("-> %s %s", method, path, extra={
            "kind": "outbound_request", "method": method, "url": url,
            "params": body_for_log(kwargs.get("params")),
            "body": body_for_log(kwargs.get("json")),
        })
        started = time.perf_counter()
        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as e:
            log.error("<- %s %s failed: %s", method, path, e, extra={
                "kind": "outbound_response", "method": method, "url": url, "error": str(e),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            raise InstagramServiceError(f"{method} {path} failed: {e}") from e
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = resp.text
        log.log(logging.INFO if resp.ok else logging.WARNING,
                "<- %s %s %s", method, path, resp.status_code, extra={
                    "kind": "outbound_response", "method": method, "url": url,
                    "status": resp.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "body": body_for_log(body),
                })
        if not resp.ok:
            raise InstagramServiceError(f"{method} {path} returned {resp.status_code}",
                                        status_code=resp.status_code, body=body)
        return body
