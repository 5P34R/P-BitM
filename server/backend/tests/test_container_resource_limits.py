import importlib
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("INTERNAL_API_KEY", "i" * 32)

from routes import campaign_common
from utils import egress_proxy, victim_containers


RESOURCE_ENV_VARS = (
    "CAMPAIGN_MEM_LIMIT",
    "CAMPAIGN_CPUS",
    "VICTIM_MEM_LIMIT",
    "VICTIM_CPUS",
    "EGRESS_MEM_LIMIT",
)

# Modules read the resource settings from the environment at import time,
# exactly like VNC_IMAGE/SELKIES_IMAGE, so env changes need a reload.
_RESOURCE_MODULES = (campaign_common, egress_proxy, victim_containers)


@contextmanager
def _resource_env(overrides=None):
    """Patch the resource env vars and reload the modules that read them."""
    patched = patch.dict(os.environ)
    patched.start()
    try:
        for name in RESOURCE_ENV_VARS:
            os.environ.pop(name, None)
        if overrides:
            os.environ.update(overrides)
        for module in _RESOURCE_MODULES:
            importlib.reload(module)
        yield
    finally:
        patched.stop()
        for module in _RESOURCE_MODULES:
            importlib.reload(module)


class ContainerResourceLimitTests(unittest.TestCase):
    def test_defaults_match_previous_hardcoded_values(self):
        with _resource_env():
            self.assertEqual(campaign_common.CAMPAIGN_MEM_LIMIT, "4g")
            self.assertEqual(campaign_common.CAMPAIGN_CPUS, 2.0)
            self.assertEqual(victim_containers.VICTIM_MEM_LIMIT, "4g")
            self.assertEqual(victim_containers.VICTIM_CPUS, 4.0)
            self.assertEqual(egress_proxy.EGRESS_MEM_LIMIT, "256m")

    def test_env_overrides_are_applied(self):
        with _resource_env({
            "CAMPAIGN_MEM_LIMIT": "1g",
            "CAMPAIGN_CPUS": "0.5",
            "VICTIM_MEM_LIMIT": "2g",
            "VICTIM_CPUS": "1.5",
            "EGRESS_MEM_LIMIT": "128m",
        }):
            self.assertEqual(campaign_common.CAMPAIGN_MEM_LIMIT, "1g")
            self.assertEqual(campaign_common.CAMPAIGN_CPUS, 0.5)
            self.assertEqual(victim_containers.VICTIM_MEM_LIMIT, "2g")
            self.assertEqual(victim_containers.VICTIM_CPUS, 1.5)
            self.assertEqual(egress_proxy.EGRESS_MEM_LIMIT, "128m")

    def test_nano_cpus_conversion(self):
        with _resource_env({"CAMPAIGN_CPUS": "0.5", "VICTIM_CPUS": "1.5"}):
            self.assertEqual(
                int(campaign_common.CAMPAIGN_CPUS * 1_000_000_000),
                500_000_000,
            )
            self.assertEqual(
                int(victim_containers.VICTIM_CPUS * 1_000_000_000),
                1_500_000_000,
            )


if __name__ == "__main__":
    unittest.main()
