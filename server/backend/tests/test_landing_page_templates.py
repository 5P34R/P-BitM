import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key-" + ("x" * 32))
os.environ.setdefault("STORAGE_PATH", tempfile.mkdtemp(prefix="bitm-test-storage-"))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401 - register all ORM models before creating the schema
from database import Base, get_db
from models.user import User
from routes.auth import router as auth_router
from routes.landing_pages import LandingPageCreate, router as landing_pages_router
from utils.auth import hash_password
from utils.landing_page import process_landing_page

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateSubstitutionTests(unittest.TestCase):
    def test_target_url_is_js_escaped(self):
        template = (TEMPLATES_DIR / "bitb_chrome.html").read_text(encoding="utf-8")
        target_url = 'https://example.com/";alert(1);//'

        rendered = process_landing_page(template, "campaign", target_url=target_url)

        self.assertIn(f"const TARGET_URL = {json.dumps(target_url)};", rendered)

    def test_template_iframe_is_reused_not_duplicated(self):
        template = (TEMPLATES_DIR / "bitb_chrome.html").read_text(encoding="utf-8")

        rendered = process_landing_page(
            template, "campaign", target_url="https://example.com/login"
        )

        self.assertEqual(rendered.count('class="iframe-visible"'), 1)
        self.assertNotIn("PROXY_URL", rendered)

    def test_placeholder_survives_without_target_url(self):
        rendered = process_landing_page(
            "<html><body>{{TARGET_URL}}</body></html>", "campaign"
        )

        self.assertIn("{{TARGET_URL}}", rendered)

    def test_proxy_url_parameter_is_rejected(self):
        with self.assertRaises(TypeError):
            process_landing_page(
                "<html><body></body></html>",
                "campaign",
                proxy_url="/stream/",
            )


class TemplateFieldValidationTests(unittest.TestCase):
    def test_accepts_safe_template_ids(self):
        request = LandingPageCreate(name="BitB", template="bitb_chrome")

        self.assertEqual(request.template, "bitb_chrome")

    def test_rejects_unsafe_template_ids(self):
        unsafe_ids = ("../etc", "BitB", "", "with-dash", "with space", "a" * 65)

        for value in unsafe_ids:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    LandingPageCreate(name="BitB", template=value)


class LandingPageTemplateEndpointTests(unittest.TestCase):
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
        app.include_router(landing_pages_router, prefix="/api/landing-pages")

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
            db.commit()

        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf_headers = {"X-CSRF-Token": response.json()["csrf_token"]}

    def test_create_loads_template_file_content(self):
        response = self.client.post(
            "/api/landing-pages",
            headers=self.csrf_headers,
            json={
                "name": "BitB landing",
                "template": "bitb_chrome",
                "content": "ignored",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        expected = (TEMPLATES_DIR / "bitb_chrome.html").read_text(encoding="utf-8")
        self.assertEqual(response.json()["content"], expected)
        self.assertIn("{{TARGET_URL}}", response.json()["content"])

    def test_create_with_unknown_template_returns_404(self):
        response = self.client.post(
            "/api/landing-pages",
            headers=self.csrf_headers,
            json={"name": "Missing", "template": "does_not_exist"},
        )

        self.assertEqual(response.status_code, 404, response.text)

    def test_create_rejects_path_traversal_template(self):
        response = self.client.post(
            "/api/landing-pages",
            headers=self.csrf_headers,
            json={"name": "Traversal", "template": "../etc"},
        )

        self.assertEqual(response.status_code, 422, response.text)

    def test_templates_list_reports_available_templates(self):
        response = self.client.get("/api/landing-pages/templates")

        self.assertEqual(response.status_code, 200, response.text)
        templates = {t["id"]: t["name"] for t in response.json()["templates"]}
        self.assertEqual(templates.get("bitb_chrome"), "BitB Chrome (fake browser window)")

    def test_templates_list_requires_auth(self):
        self.client.cookies.clear()

        response = self.client.get("/api/landing-pages/templates")

        self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
