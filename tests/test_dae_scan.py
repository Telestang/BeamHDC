from __future__ import annotations

import unittest

import beamng_hand_drive_core as core


class DaeAliasCandidateTests(unittest.TestCase):
    """dae_alias_candidates replaces a combined mesh-name regex as the
    prefilter for common DAEs. It must never be narrower than the aliases
    dae_objects_from_tree can key an object by, or meshes go missing."""

    def test_collects_both_id_and_name_attributes(self) -> None:
        data = b'<node id="acme_hood" name="Acme Hood"><matrix/></node>'
        self.assertEqual(
            core.dae_alias_candidates(data),
            {"acme_hood", "Acme Hood"},
        )

    def test_includes_stripped_forms_like_dae_node_aliases(self) -> None:
        # dae_node_aliases yields the raw and stripped value, so a node whose
        # id carries padding is reachable under the trimmed mesh name.
        data = b'<node id="  acme_door  " name=""/>'
        candidates = core.dae_alias_candidates(data)
        self.assertIn("  acme_door  ", candidates)
        self.assertIn("acme_door", candidates)

    def test_matches_the_aliases_the_object_parser_produces(self) -> None:
        import xml.etree.ElementTree as ET

        data = (
            b'<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema">'
            b'<library_visual_scenes><visual_scene>'
            b'<node id="acme_wheel" name=" acme_wheel_display ">'
            b'<matrix>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>'
            b'<instance_geometry url="#g1"/>'
            b"</node></visual_scene></library_visual_scenes></COLLADA>"
        )
        objects = core.dae_objects_from_tree(ET.ElementTree(ET.fromstring(data)), "a.dae")
        candidates = core.dae_alias_candidates(data)
        for alias in objects:
            self.assertIn(alias, candidates, f"prefilter would skip {alias!r}")

    def test_ignores_undecodable_bytes_without_raising(self) -> None:
        self.assertEqual(core.dae_alias_candidates(b'<node id="\xff\xfe"/>'), set())


if __name__ == "__main__":
    unittest.main()
