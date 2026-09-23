import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cli import commands
from cli.database import get_module_data, get_modules


PBITM_SPEC = importlib.util.spec_from_file_location(
    "pbitm_cli", PROJECT_ROOT / "p-bitm.py"
)
pbitm_cli = importlib.util.module_from_spec(PBITM_SPEC)
PBITM_SPEC.loader.exec_module(pbitm_cli)


SCHEMA = """
CREATE TABLE modules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT NOT NULL,
    icon TEXT,
    inputs TEXT,
    payload TEXT NOT NULL,
    link TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    target_url TEXT,
    mode TEXT,
    protocol TEXT,
    created_at TEXT,
    updated_at TEXT,
    status TEXT
);
CREATE TABLE victims (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    email TEXT,
    first_name TEXT,
    last_name TEXT
);
CREATE TABLE data_collections (
    id TEXT PRIMARY KEY,
    victim_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    module_id TEXT,
    data_type TEXT NOT NULL,
    file_path TEXT,
    file_size_bytes INTEGER,
    collected_at TEXT,
    extra_metadata TEXT
);
"""


def build_db(db_path, artifact_path=None):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO modules (id, name, category, description, payload) VALUES (?, ?, ?, ?, ?)",
        [
            ("modaaa11", "Keylogger", "collection", "Logs keys", "payload-a"),
            ("modbbb22", "Clipboard Hijack", "collection", "Reads clipboard", "payload-b"),
        ],
    )
    conn.execute(
        "INSERT INTO campaigns (id, name, status) VALUES (?, ?, ?)",
        ("camp0011", "Test Campaign", "active"),
    )
    conn.executemany(
        "INSERT INTO victims (id, campaign_id, email, first_name, last_name) VALUES (?, ?, ?, ?, ?)",
        [
            ("vic00111", "camp0011", "alice@example.com", "Alice", "Anderson"),
            ("vic00222", "camp0011", "bob@example.com", "Bob", "Brown"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO data_collections (
            id, victim_id, campaign_id, module_id, data_type,
            file_path, file_size_bytes, collected_at, extra_metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "data0001", "vic00111", "camp0011", "modaaa11", "module_data",
                None, None, "2026-01-01 10:00:00", json.dumps({"keys": 42}),
            ),
            (
                "data0002", "vic00111", "camp0011", None, "screenshot",
                artifact_path, 4 if artifact_path else None,
                "2026-01-02 10:00:00", None,
            ),
            (
                "data0003", "vic00222", "camp0011", "modbbb22", "module_data",
                None, None, "2026-01-03 10:00:00", json.dumps({"clips": 1}),
            ),
        ],
    )
    conn.commit()
    conn.close()


class GetModulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        build_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_modules_returns_rows_ordered_by_name(self):
        modules = get_modules(db_path=self.db_path)
        self.assertEqual([m["id"] for m in modules], ["modbbb22", "modaaa11"])
        self.assertEqual(modules[0]["name"], "Clipboard Hijack")
        self.assertEqual(modules[0]["category"], "collection")
        self.assertIn("description", modules[0])
        self.assertIn("created_at", modules[0])

    def test_get_modules_missing_database_returns_empty(self):
        modules = get_modules(db_path=Path(self.tmp.name) / "nope.db")
        self.assertEqual(modules, [])


class GetModuleDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        build_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_module_data_joins_module_and_victim(self):
        rows = get_module_data("camp0011", db_path=self.db_path)
        self.assertEqual(len(rows), 3)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["data0001"]["module_name"], "Keylogger")
        self.assertEqual(by_id["data0001"]["victim_email"], "alice@example.com")
        self.assertEqual(by_id["data0001"]["victim_first_name"], "Alice")
        self.assertEqual(by_id["data0001"]["extra_metadata"], {"keys": 42})
        self.assertIsNone(by_id["data0002"]["module_name"])
        self.assertEqual(by_id["data0003"]["module_name"], "Clipboard Hijack")

    def test_get_module_data_victim_filter(self):
        rows = get_module_data("camp0011", victim_id="vic00222", db_path=self.db_path)
        self.assertEqual([row["id"] for row in rows], ["data0003"])

    def test_get_module_data_type_filter(self):
        rows = get_module_data("camp0011", data_type="module_data", db_path=self.db_path)
        self.assertEqual(sorted(row["id"] for row in rows), ["data0001", "data0003"])

    def test_get_module_data_combined_filters(self):
        rows = get_module_data(
            "camp0011", victim_id="vic00111", data_type="screenshot",
            db_path=self.db_path,
        )
        self.assertEqual([row["id"] for row in rows], ["data0002"])

    def test_get_module_data_missing_database_returns_empty(self):
        rows = get_module_data(
            "camp0011", db_path=Path(self.tmp.name) / "nope.db"
        )
        self.assertEqual(rows, [])


class ModulesParserTests(unittest.TestCase):
    def test_modules_list_parses(self):
        parser = pbitm_cli.create_parser()
        args = parser.parse_args(["modules", "list"])
        self.assertEqual(args.command, "modules")
        self.assertEqual(args.modules_action, "list")
        self.assertEqual(args.format, "table")

    def test_modules_data_parses(self):
        parser = pbitm_cli.create_parser()
        args = parser.parse_args([
            "modules", "data",
            "--campaign", "camp0011",
            "--victim", "vic00111",
            "--type", "screenshot",
            "--out", "/tmp/export",
        ])
        self.assertEqual(args.command, "modules")
        self.assertEqual(args.modules_action, "data")
        self.assertEqual(args.campaign, "camp0011")
        self.assertEqual(args.victim, "vic00111")
        self.assertEqual(args.data_type, "screenshot")
        self.assertEqual(args.out, "/tmp/export")

    def test_modules_data_requires_campaign(self):
        parser = pbitm_cli.create_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["modules", "data"])


class ModulesDataExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.artifact = base / "screenshot.png"
        self.artifact.write_bytes(b"png!")
        self.out_dir = base / "export"
        self.rows = [
            {
                "id": "data0002",
                "data_type": "screenshot",
                "module_id": None,
                "module_name": None,
                "victim_id": "vic00111",
                "victim_email": "alice@example.com",
                "extra_metadata": None,
                "collected_at": "2026-01-02 10:00:00",
                "file_path": "screenshots/screenshot.png",
                "file_size_bytes": 4,
            },
            {
                "id": "data0004",
                "data_type": "file_hijacked",
                "module_id": "modaaa11",
                "module_name": "Keylogger",
                "victim_id": "vic00111",
                "victim_email": "alice@example.com",
                "extra_metadata": {"note": "gone"},
                "collected_at": "2026-01-04 10:00:00",
                "file_path": "files/missing.txt",
                "file_size_bytes": 0,
            },
        ]
        self.campaign = {"id": "camp0011", "name": "Test Campaign"}

    def tearDown(self):
        self.tmp.cleanup()

    def _run_export(self):
        with patch.object(commands, "get_campaign_by_id", return_value=self.campaign), \
             patch.object(commands, "get_module_data", return_value=self.rows), \
             patch.object(
                 commands,
                 "_resolve_artifact_source",
                 side_effect=lambda campaign, row: (
                     self.artifact
                     if row["file_path"] != "files/missing.txt"
                     else None
                 ),
             ):
            return commands.cmd_modules_data(
                "camp0011", out=str(self.out_dir)
            )

    def test_export_writes_json_and_copies_files(self):
        self.assertTrue(self._run_export())

        export_path = self.out_dir / "module_data.json"
        self.assertTrue(export_path.is_file())
        with open(export_path, encoding="utf-8") as handle:
            export = json.load(handle)

        self.assertEqual(export["row_count"], 2)
        self.assertEqual(export["files_copied"], 1)
        self.assertEqual(export["files_missing"], 1)

        copied = export["rows"][0]
        self.assertEqual(copied["exported_file"], "files/screenshot.png")
        self.assertEqual(copied["data_type"], "screenshot")
        self.assertEqual(copied["victim_id"], "vic00111")

        missing = export["rows"][1]
        self.assertTrue(missing["missing_file"])
        self.assertIsNone(missing["exported_file"])
        self.assertEqual(missing["module_name"], "Keylogger")
        self.assertEqual(missing["metadata"], {"note": "gone"})

        copied_file = self.out_dir / "files" / "screenshot.png"
        self.assertTrue(copied_file.is_file())
        self.assertEqual(copied_file.read_bytes(), b"png!")

    def test_export_uses_basename_only_for_copied_files(self):
        self.rows[0]["file_path"] = "../../etc/passwd"
        self.assertTrue(self._run_export())

        with open(self.out_dir / "module_data.json", encoding="utf-8") as handle:
            export = json.load(handle)
        self.assertEqual(export["rows"][0]["exported_file"], "files/passwd")
        self.assertTrue((self.out_dir / "files" / "passwd").is_file())
        self.assertFalse((self.out_dir.parent / "passwd").exists())

    def test_export_no_rows_reports_info(self):
        with patch.object(commands, "get_campaign_by_id", return_value=self.campaign), \
             patch.object(commands, "get_module_data", return_value=[]):
            self.assertTrue(commands.cmd_modules_data("camp0011", out=str(self.out_dir)))
        self.assertFalse(self.out_dir.exists())

    def test_export_unknown_campaign_returns_false(self):
        with patch.object(commands, "get_campaign_by_id", return_value=None):
            self.assertFalse(
                commands.cmd_modules_data("nope", out=str(self.out_dir))
            )


if __name__ == "__main__":
    unittest.main()
