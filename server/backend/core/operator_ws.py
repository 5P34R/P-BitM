# core/operator_ws.py

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class OperatorWSManager:
    """Fan out real-time campaign events to connected operator dashboards."""

    def __init__(self):
        self.campaigns: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, campaign_id: str, websocket: WebSocket) -> None:
        """Register an operator WebSocket for a campaign."""
        async with self._lock:
            sockets = self.campaigns.setdefault(campaign_id, set())
            sockets.add(websocket)
            total = len(sockets)
        logger.info(
            "🔌 Operator stream connected for campaign %s (total: %d)",
            campaign_id,
            total,
        )

    async def disconnect(self, campaign_id: str, websocket: WebSocket) -> bool:
        """Remove an operator WebSocket; empty campaigns are pruned."""
        async with self._lock:
            sockets = self.campaigns.get(campaign_id)
            if not sockets or websocket not in sockets:
                return False
            sockets.discard(websocket)
            if not sockets:
                self.campaigns.pop(campaign_id, None)
        logger.info("❌ Operator stream disconnected for campaign %s", campaign_id)
        return True

    async def broadcast(self, campaign_id: str, payload: dict) -> int:
        """Send a JSON payload to every socket of a campaign; prune dead ones."""
        async with self._lock:
            sockets = list(self.campaigns.get(campaign_id, ()))

        delivered = 0
        dead = []
        for websocket in sockets:
            try:
                await websocket.send_json(payload)
                delivered += 1
            except Exception as exc:
                logger.warning(
                    "⚠️ Operator stream send failed for campaign %s: %s",
                    campaign_id,
                    exc,
                )
                dead.append(websocket)

        for websocket in dead:
            await self.disconnect(campaign_id, websocket)

        return delivered

    def connection_count(self, campaign_id: str) -> int:
        return len(self.campaigns.get(campaign_id, ()))


# Singleton instance
operator_ws_manager = OperatorWSManager()
