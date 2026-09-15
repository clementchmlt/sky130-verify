from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import cli, exitcodes  # noqa: E402
from sky130_verify.manifest_cmd import (  # noqa: E402
    render_manifest_markdown, validate_manifest_file, verify_manifest_files,
)

_VALID_MANIFEST = {
    "schema_version": "1.0.0",
    "cell_id": "my_cell",
    "source": {"repository": "https://example.com/repo.git", "commit": "a" * 40,
               "paths": ["cells/my_cell"]},
    "pdk": {"family": "sky130", "variant": "sky130A", "commit_sha": "b" * 40,
            "repository": "https://github.com/RTimothyEdwards/open_pdks"},
    "toolchain": {"tools": {"magic": "8.3.500"}},
    "inputs": {"cells/my_cell/my_cell.mag": "c" * 64},
    "verification": {"drc_verdict": "pass", "lvs_verdict": "pass"},
    "artifacts": {"out/report.md": "d" * 64},
}


def _write_manifest(data: dict) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        return Path(f.name)


def _broken(**overrides_by_path) -> dict:
    return json.loads(json.dumps(_VALID_MANIFEST)) | overrides_by_path


class ManifestValidateTests(unittest.TestCase):
    def test_valid_manifest_passes_offline(self):
        ok, errors = validate_manifest_file(_write_manifest(_VALID_MANIFEST))
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_manifest_missing_required_field_is_rejected_with_a_pointer(self):
        broken = dict(_VALID_MANIFEST)
        del broken["pdk"]
        ok, errors = validate_manifest_file(_write_manifest(broken))
        self.assertFalse(ok)
        self.assertTrue(errors)

    def test_invalid_manifest_variants_are_rejected(self):
        cases = []
        merged_verdict = _broken()
        merged_verdict["verification"]["verdict"] = "clean"
        cases.append(("merged verdict", merged_verdict))
        cases.extend((
            (f"input digest {value!r}", _broken(inputs={"cells/my_cell/my_cell.mag": value}))
            for value in ("not-hex-at-all", "abc123", "g" * 64, "a" * 63, "A" * 64)
        ))
        cases.extend((
            (f"PDK commit {value!r}", _broken(pdk={**_VALID_MANIFEST["pdk"], "commit_sha": value}))
            for value in ("deadbeef", "z" * 40, "", "a" * 39)
        ))
        for field in ("drc_verdict", "lvs_verdict"):
            invalid_verdict = _broken()
            invalid_verdict["verification"][field] = "clean"
            cases.append((f"{field} enum", invalid_verdict))
        cases.extend((
            ("absolute input path", _broken(inputs={"/etc/passwd": "c" * 64})),
            ("artifact traversal", _broken(artifacts={"../../etc/passwd": "d" * 64})),
            ("non-Git source commit", _broken(source={**_VALID_MANIFEST["source"], "commit": "HEAD"})),
        ))
        for name, manifest in cases:
            with self.subTest(name=name):
                ok, errors = validate_manifest_file(_write_manifest(manifest))
                self.assertFalse(ok)
                self.assertTrue(errors)

    def test_render_markdown_includes_cell_id_and_both_verdicts_separately(self):
        text = render_manifest_markdown(_write_manifest(_VALID_MANIFEST))
        self.assertIn("my_cell", text)
        self.assertIn("DRC", text)
        self.assertIn("LVS", text)

    def test_verify_files_detects_divergent_artifact_without_running_eda(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = root / "cells" / "my_cell" / "my_cell.mag"
            report = root / "out" / "report.md"
            layout.parent.mkdir(parents=True)
            report.parent.mkdir(parents=True)
            layout.write_text("layout v1\n")
            report.write_text("report v1\n")
            manifest = _broken(
                inputs={"cells/my_cell/my_cell.mag": hashlib.sha256(layout.read_bytes()).hexdigest()},
                artifacts={"out/report.md": hashlib.sha256(report.read_bytes()).hexdigest()},
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))

            ok, errors, checked = verify_manifest_files(manifest_path, root)
            self.assertTrue(ok)
            self.assertEqual(errors, [])
            self.assertEqual(checked, 2)

            report.write_text("altered report\n")
            ok, errors, checked = verify_manifest_files(manifest_path, root)
            self.assertFalse(ok)
            self.assertEqual(checked, 2)
            self.assertTrue(any("SHA-256 mismatch" in error for error in errors))


class ManifestValidateCliJsonTests(unittest.TestCase):
    def test_json_manifest_validation_envelope(self):
        path = _write_manifest(_VALID_MANIFEST)
        out = _capture_stdout(lambda: cli.main(["manifest", "validate", str(path), "--json"]))
        data = json.loads(out)
        self.assertEqual(data["exit_code"], exitcodes.OK)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["valid"])
        self.assertEqual(data["errors"], [])
        broken = _broken()
        del broken["pdk"]
        path = _write_manifest(broken)
        out = _capture_stdout(lambda: cli.main(["manifest", "validate", str(path), "--json"]))
        data = json.loads(out)
        self.assertFalse(data["valid"])
        self.assertTrue(data["errors"])
        out = _capture_stdout(lambda: cli.main(["check", "--json"]))
        data = json.loads(out)
        self.assertEqual(data["exit_code"], exitcodes.USAGE_ERROR)
        self.assertEqual(data["status"], "usage_error")


def _capture_stdout(fn) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


class BadgeRenderCliTests(unittest.TestCase):
    def test_badge_render_writes_and_describes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest_bytes = json.dumps(_VALID_MANIFEST).encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            code = cli.main(["badge", "render", str(manifest_path)])
            self.assertEqual(code, exitcodes.OK)
            self.assertTrue((Path(tmp) / "badge.svg").is_file())
            self.assertTrue((Path(tmp) / "badge.json").is_file())
            self.assertTrue((Path(tmp) / "snippet.md").is_file())
            import hashlib
            badge_data = json.loads((Path(tmp) / "badge.json").read_text())
            self.assertEqual(badge_data["manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest())
            out = _capture_stdout(lambda: cli.main(["badge", "render", str(manifest_path), "--json"]))
            data = json.loads(out)
            self.assertEqual(data["exit_code"], exitcodes.OK)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(set(data["files"]), {"badge.svg", "badge.json", "snippet.md"})

    def test_badge_render_refuses_invalid_manifests(self):
        cases = (
            _broken(cell_id=""),
            {"verification": {"drc_verdict": "pass", "lvs_verdict": "pass"}},
        )
        for manifest in cases:
            with self.subTest(manifest=manifest):
                with tempfile.TemporaryDirectory() as tmp:
                    manifest_path = Path(tmp) / "manifest.json"
                    manifest_path.write_text(json.dumps(manifest))
                    code = cli.main(["badge", "render", str(manifest_path)])
                    self.assertEqual(code, exitcodes.NOT_CLEAN)
                    self.assertFalse((Path(tmp) / "badge.svg").is_file())


if __name__ == "__main__":
    unittest.main()
