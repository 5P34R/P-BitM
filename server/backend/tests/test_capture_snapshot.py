import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key-" + ("x" * 32))
os.environ.setdefault("STORAGE_PATH", tempfile.mkdtemp(prefix="bitm-test-storage-"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Campaign, CampaignStatus, Victim
from routes.campaign_actions import capture_snapshot
from routes.campaign_common import campaign_internal_headers


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeAsyncClient:
    calls = []
    response_payload = {"request_id": "req-123"}
    error = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, timeout=None, **kwargs):
        FakeAsyncClient.calls.append(
            {"url": url, "headers": headers, "timeout": timeout}
        )
        if FakeAsyncClient.error is not None:
            raise FakeAsyncClient.error
        return FakeResponse(FakeAsyncClient.response_payload)


class CaptureSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.campaign = Campaign(
            id="campaign",
            name="Campaign",
            target_url="https://example.test",
            container_name="p-bitm-campaign",
            status=CampaignStatus.active,
        )
        self.victim = Victim(
            id="victim",
            campaign_id=self.campaign.id,
            email="victim@example.test",
            tracking_id="tracking",
            scheduled_send_at=datetime.now(timezone.utc),
        )
        self.db.add_all([self.campaign, self.victim])
        self.db.commit()

        FakeAsyncClient.calls = []
        FakeAsyncClient.response_payload = {"request_id": "req-123"}
        FakeAsyncClient.error = None
        self.user = SimpleNamespace(username="operator")

        patcher = patch(
            "routes.campaign_actions.httpx.AsyncClient",
            FakeAsyncClient,
        )
        self.mock_client = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def call(self, campaign_id="campaign", victim_id="victim"):
        return asyncio.run(
            capture_snapshot(
                campaign_id,
                victim_id,
                db=self.db,
                current_user=self.user,
            )
        )

    def test_registers_request_with_campaign_backend(self):
        result = self.call()

        self.assertTrue(result["success"])
        self.assertEqual(result["request_id"], "req-123")

        self.assertEqual(len(FakeAsyncClient.calls), 1)
        call = FakeAsyncClient.calls[0]
        self.assertEqual(
            call["url"],
            "http://p-bitm-campaign:8080/api/sessions/victim/snapshot-requests",
        )
        self.assertEqual(
            call["headers"],
            campaign_internal_headers(self.campaign.id),
        )

    def test_unknown_campaign_is_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as context:
            self.call(campaign_id="missing")

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_unknown_victim_is_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as context:
            self.call(victim_id="missing")

        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(FakeAsyncClient.calls, [])

    def test_campaign_backend_failure_returns_502(self):
        import httpx
        from fastapi import HTTPException

        FakeAsyncClient.error = httpx.ConnectError("down")

        with self.assertRaises(HTTPException) as context:
            self.call()

        self.assertEqual(context.exception.status_code, 502)

    def test_inactive_campaign_is_rejected(self):
        from fastapi import HTTPException

        self.campaign.status = CampaignStatus.completed
        self.db.commit()

        with self.assertRaises(HTTPException) as context:
            self.call()

        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("not active", context.exception.detail)
        self.assertEqual(FakeAsyncClient.calls, [])


if __name__ == "__main__":
    unittest.main()
