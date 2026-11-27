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
    def test_owner_client_binds_map_identity_without_local_storage(self, run):
        response = {"ok": True, "sources": []}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = KnowledgeSources(root, opl_bin="/test/opl")
            self.assertEqual(client.run("sources"), response)
            command = run.call_args.args[0]
            self.assertEqual(command, ["/test/opl", "workspace", "sources", "list",
                "--workspace", str(root.resolve()), "--json"])
            self.assertEqual(list(root.iterdir()), [])

    @patch("med_autopractice.knowledge.subprocess.run")
    def test_scope_override_is_rejected_before_owner_call(self, run):
        client = KnowledgeSources(Path("/test/workspace"), opl_bin="/test/opl")
        for option in ("--workspace=/other", "--json"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                client.run("sources", [option])
        run.assert_not_called()

    @patch("med_autopractice.knowledge.subprocess.run")
    def test_owner_rejection_has_no_cache_or_automatic_retry(self, run):
        run.return_value = subprocess.CompletedProcess([], 2,
            json.dumps({"error": {"message": "synthetic private detail"}}), "")
        client = KnowledgeSources(Path("/test/workspace"), opl_bin="/test/opl")
        with self.assertRaises(KnowledgeCommandError) as caught:
            client.run("snapshot", ["--stage-run", "sr-1"])
        self.assertNotIn("synthetic private detail", str(caught.exception))
        self.assertEqual(run.call_count, 1)

    def test_direct_skill_cli_uses_opl_bin_and_forwards_exact_v2_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = "import json,sys\nprint(json.dumps({'ok':True,'arguments':sys.argv[1:]}))\n"
            if os.name == "nt":
                fake = root / "opl.cmd"
                (root / "opl.py").write_text(wrapper, encoding="utf-8")
                fake.write_text(f'@echo off\n"{sys.executable}" "%~dp0opl.py" %*\n', encoding="utf-8")
                expected_files = ["opl.cmd", "opl.py"]
            else:
                fake = root / "opl"
                fake.write_text(f"#!{sys.executable}\n{wrapper}", encoding="utf-8")
                fake.chmod(0o700)
                expected_files = ["opl"]
            result = subprocess.run([sys.executable, str(ROOT / "scripts/map_knowledge.py"),
                "--workspace", directory, "sources-add", "--path", str(root / "notes")],
                capture_output=True, text=True, env={**os.environ, "OPL_BIN": str(fake)}, check=True)
            arguments = json.loads(result.stdout)["arguments"]
            self.assertEqual(arguments[:4], ["workspace", "sources", "add", "--workspace"])
            self.assertEqual(arguments[arguments.index("--path") + 1], str(root / "notes"))
            self.assertEqual(sorted(path.name for path in root.iterdir()), expected_files)


if __name__ == "__main__":
    unittest.main()
