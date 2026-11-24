"""Phase 4 tests: offline bundler, evidence encryption/retention, session guard, and zero-PHI site metrics."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from med_autopractice.adapters.playbook_runner import ScreenObservation  # noqa: E402
from med_autopractice.adapters.session_guard import ClinicalSessionGuard, SessionLockedError  # noqa: E402
from med_autopractice.agent.task_engine import JsonlAuditSink  # noqa: E402
from med_autopractice.agent.task_store import SyntheticSqliteTaskStore  # noqa: E402
from med_autopractice.domain.task import ClinicianApproval, SubjectRef, TaskRequest  # noqa: E402
from med_autopractice.runtime import build_synthetic_runtime  # noqa: E402
from med_autopractice.storage.crypto import EnvKeyProvider, EvidenceCipher, FileKeyProvider  # noqa: E402
from med_autopractice.storage.retention import RetentionPolicy, apply_retention  # noqa: E402


class Phase4OfflineSecurityAndOpsTests(unittest.TestCase):
    def test_offline_bundle_build_verify_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            wheelhouse = tmp_dir / "wheels"
            wheelhouse.mkdir()
            (wheelhouse / "demo_optional-1.0.0-py3-none-any.whl").write_bytes(b"synthetic-wheel-bytes")

            bundle_dir = tmp_dir / "map-offline-bundle-v0.1.5"
            res = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--output", str(bundle_dir),
                "--wheelhouse", str(wheelhouse),
                "--archive", "tar.gz",
            ], capture_output=True, text=True, check=True)
            info = json.loads(res.stdout)
            self.assertTrue(Path(info["archive"]).is_file())
            self.assertEqual(info["wheelhouse_count"], 1)

            verified = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--verify", str(bundle_dir),
            ], capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(verified.stdout)["valid"])

            # Run bundled inspection inside offline bundle
            proj = subprocess.run(
                [sys.executable, str(bundle_dir / "scripts/project_status.py"), "--json"],
                cwd=tmp_dir, capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(proj.stdout)["package"]["content_integrity"], "verified")

            # Tampering one file must make --verify fail
            cap = bundle_dir / "contracts/capabilities.json"
            cap.write_text(cap.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            tampered = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--verify", str(bundle_dir),
            ], capture_output=True, text=True)
            self.assertNotEqual(tampered.returncode, 0)
            self.assertFalse(json.loads(tampered.stdout)["valid"])

            # Refuse output path inside the repository
            in_repo = subprocess.run([
                sys.executable, str(ROOT / "scripts/build_offline_bundle.py"),
                "--output", str(ROOT / "tmp_bad_bundle"),
            ], capture_output=True, text=True)
            self.assertNotEqual(in_repo.returncode, 0)

    def test_evidence_cipher_key_providers_and_tamper_resistance(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            secret_material = b"synthetic-32-byte-aes-master-key"
            key_file = tmp_dir / "evidence.key"
            key_file.write_bytes(secret_material)
            key_file.chmod(0o644)
            if os.name != "nt":
                with self.assertRaises(ValueError):
                    FileKeyProvider(key_file)
            key_file.chmod(0o600)
            provider = FileKeyProvider(key_file)

            wrong_file = tmp_dir / "wrong.key"
            wrong_file.write_bytes(b"different-32-byte-master-key-000")
            wrong_file.chmod(0o600)
            wrong_provider = FileKeyProvider(wrong_file)

            # Verify stdlib fallback path (always deterministic, zero 3P dependencies)
            cipher = EvidenceCipher(provider, prefer_aesgcm=False)
            raw_shot = b"\x89PNG\r\n\x1a\nSyntheticClinicalShotPayload"
            src = tmp_dir / "shot.png"
            enc = tmp_dir / "shot.png.enc"
            dec = tmp_dir / "shot.dec.png"
            src.write_bytes(raw_shot)
            cipher.encrypt_file(src, enc, aad=b"task-1")
            cipher.decrypt_file(enc, dec, aad=b"task-1")
            self.assertEqual(dec.read_bytes(), raw_shot)

            # Wrong key, wrong AAD, or single flipped byte in ciphertext must fail
            with self.assertRaises(ValueError):
                EvidenceCipher(wrong_provider, prefer_aesgcm=False).decrypt_bytes(enc.read_bytes(), aad=b"task-1")
            with self.assertRaises(ValueError):
                cipher.decrypt_bytes(enc.read_bytes(), aad=b"wrong-task")
            corrupted = bytearray(enc.read_bytes())
            corrupted[-1] ^= 0x01
            with self.assertRaises(ValueError):
                cipher.decrypt_bytes(bytes(corrupted), aad=b"task-1")

            # Verify EnvKeyProvider
            env_name = "MAP_TEST_KEY_MATERIAL"
            prev = os.environ.get(env_name)
            try:
                os.environ[env_name] = "synthetic-env-key-material-32-bytes"
                env_cipher = EvidenceCipher(EnvKeyProvider(env_name))
                blob = env_cipher.encrypt_bytes(b"roundtrip-data")
                self.assertEqual(env_cipher.decrypt_bytes(blob), b"roundtrip-data")
            finally:
                if prev is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = prev

    def test_retention_policy_cleanup_dry_run_and_symlink_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev_dir = Path(tmp) / "evidence"
            ev_dir.mkdir()
            old_shot = ev_dir / "old.png"
            fresh_shot = ev_dir / "fresh.png"
            old_log = ev_dir / "old.jsonl"
            for f in (old_shot, fresh_shot, old_log):
                f.write_bytes(b"12345")

            from datetime import datetime, timezone

            now = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
            old_ts = (now.timestamp() - 40 * 86400)
            fresh_ts = (now.timestamp() - 5 * 86400)
            os.utime(old_shot, (old_ts, old_ts))
            os.utime(fresh_shot, (fresh_ts, fresh_ts))
            os.utime(old_log, (old_ts, old_ts))  # 40d < 90d log retention -> must stay!

            dry = apply_retention(ev_dir, RetentionPolicy(screenshot_max_age_days=30, task_log_max_age_days=90, dry_run=True), now=now)
            self.assertEqual((dry.deleted, dry.kept), (1, 2))
            self.assertTrue(old_shot.exists())

            applied = apply_retention(ev_dir, RetentionPolicy(screenshot_max_age_days=30, task_log_max_age_days=90, dry_run=False), now=now)
            self.assertEqual((applied.deleted, applied.kept), (1, 2))
            self.assertFalse(old_shot.exists())
            self.assertTrue(fresh_shot.exists())
            self.assertTrue(old_log.exists())

            # Symlinks inside evidence directory must be rejected
            outside = Path(tmp) / "outside.png"
            outside.write_bytes(b"outside")
            (ev_dir / "link.png").symlink_to(outside)
            with self.assertRaises(ValueError):
                apply_retention(ev_dir, RetentionPolicy(), now=now)

    def test_clinical_session_guard_blocks_timeout_and_verifies_reauth(self):
        guard = ClinicalSessionGuard()
        subject = SubjectRef("demo-site", "synthetic-p09", "synthetic-e09")

        ok_obs = ScreenObservation("f1", "住院医生站 synthetic-p09 synthetic-e09", "2026-01-01T00:00:00Z")
        self.assertIsNone(guard.inspect_observation(ok_obs))
        guard.assert_can_write()

        locked_obs = ScreenObservation("f2", "提示：会话已过期，请重新登录", "2026-01-01T00:05:00Z")
        event = guard.inspect_observation(locked_obs)
        self.assertIsNotNone(event)
        self.assertEqual(guard.state, "paused_reauth_required")
        with self.assertRaises(SessionLockedError):
            guard.assert_can_write()
        with self.assertRaises(SessionLockedError):
            guard.confirm_reauthenticated(locked_obs, subject=subject)

        # Resume only after login prompt clears AND patient/encounter identity is confirmed
        wrong_pat_obs = ScreenObservation("f3", "住院医生站 other-patient synthetic-e09", "2026-01-01T00:06:00Z")
        with self.assertRaises(SessionLockedError):
            guard.confirm_reauthenticated(wrong_pat_obs, subject=subject)

        guard.confirm_reauthenticated(ok_obs, subject=subject)
        self.assertEqual(guard.state, "active")
        guard.assert_can_write()

    def test_site_metrics_computes_accurate_zero_phi_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "tasks.sqlite"
            audit = Path(tmp) / "tasks.audit.jsonl"
            rt, _ui = build_synthetic_runtime(SyntheticSqliteTaskStore(store), JsonlAuditSink(audit))

            subj = SubjectRef("demo-site", "synthetic-secret-pat-123", "synthetic-secret-enc-456")
            t1 = rt.start(TaskRequest("m-1", "revise_orders", "写医嘱1", subj, "demo://grant"))
            t1_sub = rt.submit(t1["task_id"], ClinicianApproval(t1["task_id"], "doc-1", t1["proposal"]["digest"], t1["snapshot"]["revision"]))
            rt.review(t1_sub["task_id"], "doc-1", accepted=True, digest=t1["proposal"]["digest"])

            t2 = rt.start(TaskRequest("m-2", "write_inpatient_notes", "写病程2", subj, "demo://grant"))
            rt.cancel(t2["task_id"], "clinician_changed_mind", clinician_id="doc-1")

            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts/site_metrics.py"), "--store", str(store)],
                capture_output=True, text=True, check=True,
            )
            metrics = json.loads(proc.stdout)
            self.assertEqual(metrics["total_tasks"], 2)
            self.assertEqual(metrics["status_counts"]["accepted"], 1)
            self.assertEqual(metrics["status_counts"]["cancelled"], 1)
            self.assertEqual(metrics["first_pass_acceptance_rate"], 1.0)
            self.assertEqual(metrics["cancel_rate"], 0.5)
            self.assertNotIn("synthetic-secret-pat-123", proc.stdout)
            self.assertNotIn("synthetic-secret-enc-456", proc.stdout)
