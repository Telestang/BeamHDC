from __future__ import annotations

import unittest

import beamng_hand_drive_core as core
import beamng_transform_helpers as th


# Stock content ships part keys with a stray comma between the colon and the
# brace ("bluebuck_bumper_F":, {...}); the game's lenient parser accepts it.
STRAY_COMMA_PART = """
{
"acme_bumper_F":, {
    "information":{
        "authors":"BeamNG",
        "name":"Front Bumper",
    },
    "slotType" : "acme_bumper_F",
    "slots": [
        ["type", "default", "description"],
        ["acme_bumperguards_F","", "Front Bumper Guards"],
    ],
    "flexbodies": [
         ["mesh", "[group]:"],
         ["acme_bumper_F", ["acme_bumper_F"]],
    ],
},
}
"""

# pickup_bumper_F_prefacelift.jbeam ships a commented-out slots2 row with an
# unbalanced quote; the whole vehicle used to fail with "Unclosed [] block".
MALFORMED_COMMENTED_ROW_ARRAY = """[
    ["name", "allowTypes", "denyTypes", "default", "description"],
    ["acme_lip_F", ["acme_lip_F"], [], "", "Front Lip"],
    //["acme_guards_F"","","Front Bumper Guards"]
]"""


class StrayCommaPartKeyTests(unittest.TestCase):
    def test_part_body_index_tolerates_stray_comma(self) -> None:
        index = core.build_part_body_index({"vehicles/acme/a.jbeam": STRAY_COMMA_PART})
        self.assertIn("acme_bumper_F", index)
        body, filename = index["acme_bumper_F"]
        self.assertEqual(filename, "vehicles/acme/a.jbeam")
        self.assertIn('"slotType"', body)

    def test_extract_keyed_object_tolerates_stray_comma(self) -> None:
        body = th.extract_keyed_object(STRAY_COMMA_PART, "acme_bumper_F")
        self.assertIsNotNone(body)
        self.assertIn('"slotType"', body)

    def test_named_array_tolerates_stray_comma(self) -> None:
        text = '{"nodes":, [\n["id", "posX", "posY", "posZ"],\n["n1", 0.1, 0.2, 0.3],\n]}'
        array = th.extract_named_array(text, "nodes")
        self.assertIsNotNone(array)
        self.assertEqual(
            core.extract_node_positions_from_array(array),
            {"n1": (0.1, 0.2, 0.3)},
        )

    def test_node_position_index_tolerates_stray_comma(self) -> None:
        text = '{"part": {"nodes":,[\n["id", "posX", "posY", "posZ"],\n["n1", 1.0, 2.0, 3.0],\n]}}'
        nodes = core.build_node_position_index({"vehicles/acme/a.jbeam": text})
        self.assertEqual(nodes, {"n1": (1.0, 2.0, 3.0)})


class MalformedRowTests(unittest.TestCase):
    def test_iter_top_level_rows_skips_unbalanced_commented_row(self) -> None:
        rows = core.iter_top_level_rows(MALFORMED_COMMENTED_ROW_ARRAY)
        self.assertEqual(len(rows), 2)
        self.assertIn('"acme_lip_F"', rows[1])

    def test_iter_active_top_level_rows_skips_unbalanced_commented_row(self) -> None:
        rows = core.iter_active_top_level_rows(MALFORMED_COMMENTED_ROW_ARRAY)
        self.assertEqual(len(rows), 2)
        self.assertIn('"acme_lip_F"', rows[1])

    def test_slot_demand_types_survives_malformed_slots2_row(self) -> None:
        body = f'"acme_part": {{\n"slotType": "main",\n"slots2": {MALFORMED_COMMENTED_ROW_ARRAY},\n}}'
        self.assertEqual(core.slot_demand_types(body), {"acme_lip_F"})

    def test_well_formed_commented_rows_are_still_returned(self) -> None:
        # The build path deliberately keeps commented-out rows verbatim.
        array = '[\n["a", 1],\n//["b", 2]\n]'
        rows = core.iter_top_level_rows(array)
        self.assertEqual(rows, ['["a", 1]', '["b", 2]'])

    def test_slot_defs_ignore_commented_out_rows(self) -> None:
        # bluebuck ships //["bluebuck_","bluebuck_", ""] slot rows; the game
        # does not load them, so they must not select phantom parts.
        body = (
            '"acme_bumperguards_F": {\n'
            '"slotType": "acme_bumperguards_F",\n'
            '"slots": [\n'
            '    ["type", "default", "description"],\n'
            '    //["acme_","acme_", ""],\n'
            '    ["acme_trim_F", "acme_trim_F_chrome", "Trim"],\n'
            "],\n"
            "}"
        )
        defs = core.extract_slot_defs(body)
        self.assertEqual(
            [(d.slot_type, d.default_part) for d in defs],
            [("acme_trim_F", "acme_trim_F_chrome")],
        )
        self.assertEqual(core.slot_demand_types(body), {"acme_trim_F"})


if __name__ == "__main__":
    unittest.main()
