import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cli import utils as cli_utils
from cli.config import Config


class GenerateEnvResourceLimitsTests(unittest.TestCase):
    """generate_env_file() must expose the docker.resources config values as
    env vars so the backend containers pick them up, without touching the
    real project .env (the test redirects the project root to a temp copy)."""

    def _generate_env(self, config_yaml=None, existing_env=None):
        tmp_root = Path(tempfile.mkdtemp(prefix="pbitm-env-test-"))
        (tmp_root / "cli").mkdir()
        (tmp_root / "server").mkdir()
        storage = tmp_root / "storage"
        storage.mkdir()

        config_path = tmp_root / "config.yaml"
        if config_yaml is not None:
            config_path.write_text(config_yaml)

        env_file = tmp_root / "server" / ".env"
        if existing_env is not None:
            env_file.write_text(existing_env)

        with patch.object(
            cli_utils, "__file__", str(tmp_root / "cli" / "utils.py")
        ), patch.object(cli_utils, "config", Config(config_path)), patch.object(
            cli_utils,
            "ensure_storage_directories",
            return_value=(storage, storage / "campaigns"),
        ), patch.object(
            cli_utils,
            "resolve_runtime_identity",
            return_value=SimpleNamespace(uid=1000, gid=1000),
        ), patch.object(
            cli_utils, "adopt_generated_path", lambda path: None
        ):
            cli_utils.generate_env_file()

        values = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values

    def test_resource_limits_default_to_previous_hardcoded_values(self):
        values = self._generate_env()
        self.assertEqual(values["CAMPAIGN_MEM_LIMIT"], "4g")
        self.assertEqual(values["CAMPAIGN_CPUS"], "2")
        self.assertEqual(values["VICTIM_MEM_LIMIT"], "4g")
        self.assertEqual(values["VICTIM_CPUS"], "4")
        self.assertEqual(values["EGRESS_MEM_LIMIT"], "256m")

    def test_config_yaml_resource_limits_land_in_env_file(self):
        values = self._generate_env(config_yaml=(
            "docker:\n"
            "  resources:\n"
            "    campaign_mem_limit: \"1g\"\n"
            "    campaign_cpus: 0.5\n"
            "    victim_mem_limit: \"2g\"\n"
            "    victim_cpus: 1\n"
            "    egress_mem_limit: \"128m\"\n"
        ))
        self.assertEqual(values["CAMPAIGN_MEM_LIMIT"], "1g")
        self.assertEqual(values["CAMPAIGN_CPUS"], "0.5")
        self.assertEqual(values["VICTIM_MEM_LIMIT"], "2g")
        self.assertEqual(values["VICTIM_CPUS"], "1")
        self.assertEqual(values["EGRESS_MEM_LIMIT"], "128m")

    def test_existing_credentials_are_preserved(self):
        admin_password = "A-secure-existing-password-123"
        internal_key = "k" * 40
        encryption_key = "e" * 40
        values = self._generate_env(existing_env=(
            f"ADMIN_PASSWORD={admin_password}\n"
            f"INTERNAL_API_KEY={internal_key}\n"
            f"DATA_ENCRYPTION_KEY={encryption_key}\n"
        ))
        self.assertEqual(values["ADMIN_PASSWORD"], admin_password)
        self.assertEqual(values["INTERNAL_API_KEY"], internal_key)
        self.assertEqual(values["DATA_ENCRYPTION_KEY"], encryption_key)


if __name__ == "__main__":
    unittest.main()
