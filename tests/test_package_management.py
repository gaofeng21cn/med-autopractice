"""The native carrier must carry a runnable, truthful MAP package."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageManagementTests(unittest.TestCase):
    def materialize(self, target):
        package = json.loads((ROOT / "opl-package.json").read_text())
        for relative in ["opl-package.json", *package["content_lock"]["paths"]]:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)

    def test_materialized_package_runs_without_developer_checkout(self):
        with tempfile.TemporaryDirectory(prefix="map-package-") as temporary:
            target = Path(temporary)
            self.materialize(target)
            result = subprocess.run([sys.executable, str(target / "scripts/project_status.py"), "--json"],
                                    cwd=target.parent, capture_output=True, text=True, check=True)
            status = json.loads(result.stdout)
            self.assertEqual(status["package"]["content_integrity"], "verified")
            self.assertEqual(status["package"]["package_id"], "med-autopractice")
            self.assertFalse(status["clinical_execution_available"])
            self.assertEqual(status["callable_capabilities"], [])
            help_result = subprocess.run([sys.executable, str(target / "scripts/map_runtime.py"), "--help"],
                                         cwd=target.parent, capture_output=True, text=True, check=True)
            self.assertIn("synthetic", help_result.stdout)

    def test_materialized_package_rejects_changed_content(self):
        with tempfile.TemporaryDirectory(prefix="map-package-") as temporary:
            target = Path(temporary)
            self.materialize(target)
            capabilities = target / "contracts/capabilities.json"
            capabilities.write_text(capabilities.read_text() + "\n")
            result = subprocess.run([sys.executable, str(target / "scripts/project_status.py"), "--json"],
                                    cwd=target, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("content lock", result.stderr)


if __name__ == "__main__":
    unittest.main()
