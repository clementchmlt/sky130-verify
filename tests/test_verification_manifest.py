from __future__ import annotations

from pathlib import Path
import sys
import unittest

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify.verification_manifest import (
    MANIFEST_JSON_SCHEMA,
    ManifestValidationError,
    Pdk,
    Source,
    Verification,
    VerificationManifest,
)


SHA = "a" * 64
GIT = "b" * 40


def example_manifest() -> VerificationManifest:
    return VerificationManifest(
        schema_version="1.0.0",
        cell_id="opamp_core",
        source=Source("https://github.com/example/opamp", GIT, ("layout/opamp_core.mag", "schematic/opamp_core.spice")),
        pdk=Pdk("sky130", "sky130A", GIT, "https://github.com/google/skywater-pdk"),
        toolchain={"image": "example@sha256:" + SHA, "tools": {"magic": "8.3.0", "netgen": "1.5.0"}},
        inputs={"layout/opamp_core.mag": SHA},
        verification=Verification("pass", "fail", "logs/drc.log", "logs/lvs.log"),
        artifacts={"out/opamp_core.ext": SHA},
    )


class VerificationManifestTests(unittest.TestCase):
    def test_model_serializes_exactly_eight_top_level_fields_and_validates_schema(self) -> None:
        rendered = example_manifest().to_dict()
        self.assertEqual(list(rendered), ["schema_version", "cell_id", "source", "pdk", "toolchain", "inputs", "verification", "artifacts"])
        jsonschema.Draft202012Validator(MANIFEST_JSON_SCHEMA).validate(rendered)
        self.assertEqual(rendered["verification"]["drc_verdict"], "pass")
        self.assertEqual(rendered["verification"]["lvs_verdict"], "fail")

    def test_absolute_paths_and_missing_pdk_commit_are_rejected(self) -> None:
        with self.assertRaises(ManifestValidationError):
            Source("https://github.com/example/opamp", GIT, ("/tmp/layout.mag",))
        with self.assertRaises(ManifestValidationError):
            Pdk("sky130", "sky130A", "unknown", "https://github.com/google/skywater-pdk")


if __name__ == "__main__":
    unittest.main()
