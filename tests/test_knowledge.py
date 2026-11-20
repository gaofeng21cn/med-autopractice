import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from med_autopractice.knowledge import DOMAIN_ID, KnowledgeCommandError, KnowledgeSources


ROOT = Path(__file__).resolve().parents[1]


class KnowledgeSourcesTests(unittest.TestCase):
    @patch("med_autopractice.knowledge.subprocess.run")
    def test_owner_client_binds_map_identity_and_project_without_local_storage(self, run):
        response = {"version": "g2", "knowledge_sources": {"items": [], "quality_debt": []}}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = KnowledgeSources(root, project=root / "project", opl_bin="/test/opl")
            self.assertEqual(client.run("sources"), response)
            command = run.call_args.args[0]
            self.assertEqual(command, ["/test/opl", "knowledge", "sources",
                "--workspace", str(root.resolve()), "--domain", DOMAIN_ID,
                "--project", str((root / "project").resolve()), "--json"])
            self.assertEqual(list(root.iterdir()), [])

    @patch("med_autopractice.knowledge.subprocess.run")
    def test_scope_override_is_rejected_before_owner_call(self, run):
        client = KnowledgeSources(Path("/test/workspace"), opl_bin="/test/opl")
        for option in ("--domain", "--project=/other", "--workspace=/other", "--json"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                client.run("sources", [option])
        run.assert_not_called()

    @patch("med_autopractice.knowledge.subprocess.run")
    def test_owner_rejection_has_no_cache_or_automatic_retry(self, run):
        run.return_value = subprocess.CompletedProcess([], 2,
            json.dumps({"version": "g2", "error": {"message": "synthetic private detail"}}), "")
        client = KnowledgeSources(Path("/test/workspace"), opl_bin="/test/opl")
        with self.assertRaises(KnowledgeCommandError) as caught:
            client.run("read-source", ["--id", "test.policy"])
        self.assertNotIn("synthetic private detail", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    def test_direct_skill_cli_uses_opl_bin_and_forwards_exact_review_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "opl"
            fake.write_text("#!" + sys.executable + "\nimport json,sys\n"
                "print(json.dumps({'version':'g2','arguments':sys.argv[1:]}))\n")
            fake.chmod(0o700)
            result = subprocess.run([sys.executable, str(ROOT / "scripts/map_knowledge.py"),
                "--workspace", directory, "register", "--registry", str(root / "registry.json"),
                "--item", str(root / "item.json"), "--expected-sha256", "sha256:synthetic",
                "--reviewer", "synthetic-owner", "--review-ref", "synthetic://review",
                "--reviewed-at", "2026-11-09T00:00:00Z", "--next-review-at", "2027-11-09T00:00:00Z"],
                capture_output=True, text=True, env={**os.environ, "OPL_BIN": str(fake)}, check=True)
            arguments = json.loads(result.stdout)["arguments"]
            self.assertEqual(arguments[:3], ["knowledge", "register", "--workspace"])
            self.assertEqual(arguments[arguments.index("--domain") + 1], DOMAIN_ID)
            self.assertEqual(arguments[arguments.index("--review-ref") + 1], "synthetic://review")
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["opl"])


if __name__ == "__main__":
    unittest.main()
