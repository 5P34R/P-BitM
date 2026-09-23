import json
import re
import unittest
from pathlib import Path


MODULES_DIR = Path(__file__).resolve().parents[1] / "modules"
TARGET_MODULES = (
    "clipboard-hijack.json",
    "mfa-relay.json",
    "bot-guard.json",
    "fake-cloudflare-challenge.json",
)

PARAM_PLACEHOLDER_RE = re.compile(r"\{\{\s*params\[(\d+)\]\s*\}\}")
MODULE_ID_RE = re.compile(r"\{\{\s*module_id\s*\}\}")
SCRIPT_SRC_RE = re.compile(r"<script[^>]*\ssrc\s*=", re.IGNORECASE)
ABSOLUTE_FETCH_RE = re.compile(r"fetch\(\s*['\"]https?://", re.IGNORECASE)


def iter_module_files():
    return sorted(
        path for path in MODULES_DIR.glob("*.json") if not path.name.startswith("_")
    )


def load_module(path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


class ModuleSchemaTests(unittest.TestCase):
    def test_modules_parse_and_have_core_fields(self):
        for path in iter_module_files():
            with self.subTest(module=path.name):
                module = load_module(path)
                self.assertIsInstance(module.get("name"), str)
                self.assertTrue(module["name"].strip())
                self.assertIsInstance(module.get("payload"), str)
                self.assertTrue(module["payload"].strip())
                self.assertTrue(str(module.get("category", "")).strip())
                self.assertIn("link", module)

    def test_input_ids_are_unique(self):
        for path in iter_module_files():
            with self.subTest(module=path.name):
                module = load_module(path)
                ids = [item["id"] for item in module.get("inputs", [])]
                self.assertEqual(len(ids), len(set(ids)))

    def test_placeholders_reference_declared_inputs(self):
        for path in iter_module_files():
            with self.subTest(module=path.name):
                module = load_module(path)
                declared = {item["id"] for item in module.get("inputs", [])}
                used = {
                    int(match)
                    for match in PARAM_PLACEHOLDER_RE.findall(module["payload"])
                }
                self.assertTrue(
                    used <= declared,
                    f"undeclared params used: {sorted(used - declared)}",
                )

    def test_no_external_script_tags(self):
        for path in iter_module_files():
            with self.subTest(module=path.name):
                module = load_module(path)
                self.assertIsNone(SCRIPT_SRC_RE.search(module["payload"]))

    def test_module_data_posts_include_module_id_templating(self):
        for path in iter_module_files():
            with self.subTest(module=path.name):
                module = load_module(path)
                payload = module["payload"]
                if "module_data" not in payload:
                    continue
                self.assertIn("data_type", payload)
                self.assertRegex(payload, MODULE_ID_RE)


class CollectorPayloadTests(unittest.TestCase):
    def test_target_modules_use_relative_collector_link(self):
        for name in TARGET_MODULES:
            with self.subTest(module=name):
                module = load_module(MODULES_DIR / name)
                self.assertEqual(module["link"], "c/{{victim_id}}")
                self.assertIsNone(ABSOLUTE_FETCH_RE.search(module["payload"]))


class ClipboardHijackModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module(MODULES_DIR / "clipboard-hijack.json")
        cls.payload = cls.module["payload"]

    def test_declared_metadata(self):
        self.assertEqual(self.module["name"], "Clipboard Hijacker")
        self.assertEqual(self.module["category"], "Clipboard Hijacking")
        labels = [item["label"] for item in self.module["inputs"]]
        self.assertEqual(
            labels,
            [
                "Lure Text",
                "Payload To Plant",
                "Read Clipboard Back (true/false)",
            ],
        )

    def test_plants_clipboard_with_legacy_fallback(self):
        self.assertIn("navigator.clipboard.writeText", self.payload)
        self.assertIn("execCommand('copy')", self.payload)

    def test_optional_clipboard_readback(self):
        self.assertIn("navigator.clipboard.readText", self.payload)

    def test_params_embedded_via_hidden_dom_elements(self):
        self.assertIn('id="cbPayload" hidden', self.payload)
        self.assertIn('id="cbReadBack" hidden', self.payload)
        self.assertIn("textContent", self.payload)

    def test_posts_module_data_metadata(self):
        self.assertIn("data_type:'module_data'", self.payload)
        self.assertIn("planted:true", self.payload)
        self.assertIn("clipboard:clip", self.payload)


class MfaRelayModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module(MODULES_DIR / "mfa-relay.json")
        cls.payload = cls.module["payload"]

    def test_declared_metadata(self):
        self.assertEqual(self.module["category"], "MFA Relay")

    def test_posts_module_data_metadata(self):
        self.assertIn("data_type:'module_data'", self.payload)
        self.assertIn("module_id", self.payload)


class AntiBotModuleTests(unittest.TestCase):
    """Shared assertions for the Anti-Bot overlay modules."""

    modules = ("bot-guard.json", "fake-cloudflare-challenge.json")

    def test_declared_metadata(self):
        for name in self.modules:
            with self.subTest(module=name):
                module = load_module(MODULES_DIR / name)
                self.assertEqual(module["category"], "Anti-Bot")

    def test_full_viewport_opaque_overlay(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("position:fixed", payload)
                self.assertIn("inset:0", payload)

    def test_reveals_page_via_remove_module(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("__removeModule()", payload)

    def test_decoy_redirect_for_bots(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("window.location.href=decoy", payload)

    def test_probes_navigator_webdriver(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("navigator.webdriver", payload)

    def test_params_embedded_via_hidden_dom_elements(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn(" hidden>{{ params[", payload)
                self.assertIn("textContent", payload)

    def test_no_absolute_fetch_urls(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIsNone(ABSOLUTE_FETCH_RE.search(payload))

    def test_posts_module_data_with_verdict(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("data_type:'module_data'", payload)
                self.assertIn("verdict", payload)
                self.assertIn("signals", payload)

    def test_fail_open_hard_timeout(self):
        for name in self.modules:
            with self.subTest(module=name):
                payload = load_module(MODULES_DIR / name)["payload"]
                self.assertIn("setTimeout(reveal", payload)
                self.assertIn("catch", payload)


class BotGuardModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module(MODULES_DIR / "bot-guard.json")
        cls.payload = cls.module["payload"]

    def test_declared_metadata(self):
        self.assertEqual(self.module["name"], "Bot Guard (Browser Check)")
        labels = [item["label"] for item in self.module["inputs"]]
        self.assertEqual(
            labels,
            [
                "Decoy URL (redirect if bot detected)",
                "Challenge Timeout (seconds)",
            ],
        )

    def test_instant_fail_on_headless_or_webdriver(self):
        self.assertIn("headlesschrome", self.payload)
        self.assertIn("score:100", self.payload)

    def test_timeout_default_fallback(self):
        self.assertIn("||10", self.payload)


class FakeCloudflareChallengeModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module(MODULES_DIR / "fake-cloudflare-challenge.json")
        cls.payload = cls.module["payload"]

    def test_declared_metadata(self):
        self.assertEqual(self.module["name"], "Fake Cloudflare Challenge")
        labels = [item["label"] for item in self.module["inputs"]]
        self.assertEqual(
            labels,
            [
                "Minimum Wait (seconds)",
                "Decoy URL (redirect if bot detected)",
                "Run Bot Check (true/false)",
            ],
        )

    def test_cloudflare_branding_present(self):
        self.assertIn("Just a moment...", self.payload)
        self.assertIn("Performance & security by", self.payload)
        self.assertIn("Cloudflare", self.payload)
        self.assertIn("Ray ID: ", self.payload)

    def test_wait_default_fallback(self):
        self.assertIn("||5", self.payload)


if __name__ == "__main__":
    unittest.main()
