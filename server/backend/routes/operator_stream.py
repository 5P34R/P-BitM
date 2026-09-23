"""Real-time operator event stream (WebSocket)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from core.operator_ws import operator_ws_manager
from database import get_db
from models.campaign import Campaign
from models.user import User
from models.user_session import UserSession
from utils.permissions import VALID_USER_ROLES, can_access_campaign
from utils.session_auth import SESSION_COOKIE_NAME, hash_secret

logger = logging.getLogger(__name__)

router = APIRouter()

WS_CLOSE_POLICY_VIOLATION = 1008


async def _authenticate_operator(websocket: WebSocket, db: Session) -> User | None:
    """Validate the dashboard session cookie presented during the handshake.

    Mirrors the REST session checks in routes/auth.py. The WebSocket
    handshake is a GET, so the CSRF check does not apply here.
    """
    raw_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token or len(raw_token) > 256:
        return None

    session = (
        db.query(UserSession)
        .filter(UserSession.token_hash == hash_secret(raw_token))
        .first()
    )
    if session is None or session.expires_at <= datetime.now(timezone.utc):
        return None

    user = db.query(User).filter(User.id == session.user_id).first()
    if not user or not user.is_active or user.role not in VALID_USER_ROLES:
        return None
    return user


@router.websocket("/{campaign_id}/stream")
async def operator_stream(
    websocket: WebSocket,
    campaign_id: str,
    db: Session = Depends(get_db),
):
    """Stream live campaign events (module_data, ...) to operator dashboards."""
    user = await _authenticate_operator(websocket, db)
    if user is None:
        logger.warning(
            "🔒 Rejected operator stream for campaign %s: invalid or missing session",
            campaign_id,
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return

    campaign = (
        db.query(Campaign)
        .filter(
            Campaign.id == campaign_id,
            Campaign.deleted_at == None,
        )
        .first()
    )
    if not campaign or not can_access_campaign(user, campaign):
        logger.warning(
            "🔒 Rejected operator stream for campaign %s from user %s",
            campaign_id,
            user.id,
        )
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    await operator_ws_manager.connect(campaign_id, websocket)
    logger.info(
        "📡 Operator stream opened for campaign %s by user %s",
        campaign_id,
        user.username,
    )
    try:
        while True:
            # Operators only receive events; drain inbound frames so a
            # client drop surfaces as WebSocketDisconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await operator_ws_manager.disconnect(campaign_id, websocket)
        logger.info("📡 Operator stream closed for campaign %s", campaign_id)
