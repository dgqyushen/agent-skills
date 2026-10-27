"""Unit tests for the input-token-overheads audit script (stdlib only, no network)."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "audit_overheads.py"
_spec = importlib.util.spec_from_file_location("audit_overheads", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class HealthRatio(unittest.TestCase):
    def test_ratings(self):
        def rating(tokens):
            pct, r = mod.health_ratio(tokens)
            return r
        self.assertEqual(rating(9_000), "excellent")
        self.assertEqual(rating(29_000), "healthy")
        self.assertEqual(rating(31_000), "acceptable")
        self.assertEqual(rating(51_000), "unhealthy")


class CheckFile(unittest.TestCase):
    def test_status_by_cap(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "USER.md"
            p.write_text("x" * 2999)
            self.assertEqual(mod.check_file(str(p), 3000, "u")["status"], "OK")
            p.write_text("x" * 3001)
            self.assertEqual(mod.check_file(str(p), 3000, "u")["status"],
                             "TRUNCATED")
            self.assertEqual(mod.check_file(str(Path(d) / "nope"), 3000,
                                            "u")["status"], "MISSING")


class InjectedFiles(unittest.TestCase):
    def test_profile_scan_respects_envless_default(self):
        with tempfile.TemporaryDirectory() as d:
            old = mod.HERMES
            try:
                mod.HERMES = d
                for p in (d, Path(d) / "memories"):
                    Path(p).mkdir(parents=True, exist_ok=True)
                (Path(d) / "memories" / "USER.md").write_text("y" * 100)
                out = mod.audit_injected_files()
                rows = {f["file"]: f for f in out}
                self.assertIn("user profile", rows)
                self.assertEqual(rows["user profile"]["status"], "OK")
            finally:
                mod.HERMES = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
