from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sky130_verify import manifest_backend  # noqa: E402
from sky130_verify.manifest_backend import (  # noqa: E402
    MANIFEST_JSON_SCHEMA,
    Pdk,
    Source,
    Verification,
    VerificationManifest,
)

from sky130_verify import verification_manifest as _direct  # noqa: E402


class ManifestBackendTests(unittest.TestCase):
    def test_backend_reexports_the_same_schema_object_not_a_copy(self):
        self.assertEqual(MANIFEST_JSON_SCHEMA, _direct.MANIFEST_JSON_SCHEMA)
        self.assertIs(manifest_backend.VerificationManifest, _direct.VerificationManifest)

    def test_round_trip_manifest_is_schema_valid(self):
        import jsonschema

        manifest = VerificationManifest(
            schema_version="1.0.0",
            cell_id="sky130_fd_pr__nfet_01v8",
            source=Source(repository="https://example.com/repo.git", commit="a" * 40,
                           paths=("cells/nfet",)),
            pdk=Pdk(family="sky130", variant="sky130A", commit_sha="b" * 40,
                    repository="https://github.com/RTimothyEdwards/open_pdks"),
            toolchain={"tools": {"magic": "8.3.500"}},
            inputs={"cells/nfet/nfet.mag": "c" * 64},
            verification=Verification(drc_verdict="pass", lvs_verdict="pass"),
            artifacts={"out/report.md": "d" * 64},
        )
        jsonschema.Draft202012Validator(MANIFEST_JSON_SCHEMA).validate(manifest.to_dict())


if __name__ == "__main__":
    unittest.main()
