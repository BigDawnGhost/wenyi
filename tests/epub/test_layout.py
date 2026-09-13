from __future__ import annotations

import unittest

from trans_novel.epub.layout import (
    LAYOUT_POLICY_VERSION,
    LayoutAssignment,
    LayoutProfile,
    source_node_digest,
)

_SHA = "a" * 64


class TestLayoutContracts(unittest.TestCase):
    def _profile(self, **changes):
        values = {
            "source_sha256": _SHA,
            "inventory_digest": "b" * 64,
            "policy_version": LAYOUT_POLICY_VERSION,
            "assignments": (LayoutAssignment("n", "c.xhtml", (1, 2), "c" * 64, "heading", 2),),
            "provenance": {"model": "fake", "timestamp": "now"},
        }
        values.update(changes)
        return LayoutProfile(**values)

    def test_profile_round_trip_is_strict_and_digest_ignores_provenance(self):
        profile = self._profile()
        restored = LayoutProfile.from_dict(profile.to_dict())
        self.assertEqual(restored, profile)
        self.assertEqual(profile.digest, self._profile(provenance={"usage": 99}).digest)

        malformed = profile.to_dict()
        malformed["rules"] = "legacy"
        with self.assertRaisesRegex(ValueError, "schema"):
            LayoutProfile.from_dict(malformed)
        malformed = profile.to_dict()
        malformed["assignments"][0]["path"] = [True]
        with self.assertRaisesRegex(ValueError, "integer array"):
            LayoutProfile.from_dict(malformed)
        for provenance in ({1: "not a JSON object key"}, {"tuple": ("not", "JSON")}):
            with (
                self.subTest(provenance=provenance),
                self.assertRaisesRegex(ValueError, "JSON-compatible"),
            ):
                self._profile(provenance=provenance)

    def test_rejects_duplicate_identity_roles_and_invalid_levels(self):
        assignment = LayoutAssignment("n", "c.xhtml", (1,), "c" * 64, None)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self._profile(assignments=(assignment, assignment))
        for role, level in (("unknown", None), (None, 1), ("body", 1), ("heading", None)):
            with self.subTest(role=role, level=level), self.assertRaises(ValueError):
                LayoutAssignment("x", "c.xhtml", (0,), "d" * 64, role, level)

    def test_source_digest_canonicalizes_attribute_order_but_covers_all_source_fields(self):
        digest = source_node_digest("p", {"class": "quote", "id": "x"}, "full text")
        self.assertEqual(
            digest,
            source_node_digest("p", {"id": "x", "class": "quote"}, "full text"),
        )
        self.assertNotEqual(digest, source_node_digest("p", {"class": "quote"}, "full text"))
        self.assertNotEqual(
            digest,
            source_node_digest("p", {"class": "quote", "id": "x"}, "changed"),
        )


if __name__ == "__main__":
    unittest.main()
