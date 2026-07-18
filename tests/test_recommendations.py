from __future__ import annotations

import unittest
from pathlib import Path

import beamng_hand_drive_core as core
import beamng_hand_drive_tool as tool


def make_context(vehicle_id: str, object_ids: dict[str, float]) -> core.VehicleContext:
    objects = {
        object_id: core.DaeObject(
            id=object_id,
            name=object_id,
            dae_path="vehicle.dae",
            x=x,
            y=0.0,
            z=0.0,
            geometry_ids=(),
        )
        for object_id, x in object_ids.items()
    }
    return core.VehicleContext(
        source_zip=Path("test.zip"),
        vehicle_id=vehicle_id,
        vehicle_path=f"vehicles/{vehicle_id}",
        dae_paths=[],
        variants={},
        objects=objects,
        preview_by_id={},
        jbeam_texts={},
        node_positions={},
        project_dir=Path("project"),
    )


def recommend(object_ids: dict[str, float]) -> dict[str, dict[str, object]]:
    context = make_context("etk800", object_ids)
    recs = tool.build_mode_recommendations(context, list(object_ids))
    return {r["object_id"]: r for r in recs}


class RecommendationTests(unittest.TestCase):
    def test_plain_steer_name_gets_translate(self) -> None:
        recs = recommend({"etk800_steer": 0.4})
        self.assertEqual(recs["etk800_steer"]["mode"], core.MODE_TRANSLATE)
        self.assertEqual(recs["etk800_steer"]["reason"], "steering wheel")

    def test_screen_gets_mirror_with_texture_flip(self) -> None:
        recs = recommend({"etk800_screen": 0.0})
        self.assertEqual(recs["etk800_screen"]["mode"], core.MODE_MIRROR)
        self.assertTrue(recs["etk800_screen"]["textureFlip"])

    def test_gauges_screen_stays_translate_and_windscreen_is_skipped(self) -> None:
        recs = recommend({"etk800_gauges_screen": 0.4, "etk800_windscreen": 0.0})
        self.assertEqual(recs["etk800_gauges_screen"]["mode"], core.MODE_TRANSLATE)
        self.assertNotIn("etk800_windscreen", recs)

    def test_headliner_and_sunvisor_get_no_recommendation(self) -> None:
        recs = recommend({"etk800_headliner": 0.0, "etk800_sunvisor": 0.0})
        self.assertEqual(recs, {})

    def test_unpaired_seat_hardware_gets_mirror(self) -> None:
        recs = recommend({"racingseat_base": 0.0})
        self.assertEqual(recs["racingseat_base"]["mode"], core.MODE_MIRROR)

    def test_rear_bench_seats_get_no_recommendation(self) -> None:
        # etk800_seats_R: R means rear, not right; the bench is symmetric.
        recs = recommend({"etk800_seats_R": 0.0})
        self.assertEqual(recs, {})

    def test_paired_seats_get_structural_pair(self) -> None:
        recs = recommend({"etk800_seat_FL": 0.4, "etk800_seat_FR": -0.4})
        self.assertEqual(recs["etk800_seat_FL"]["mode"], core.MODE_MIRROR_STRUCTURAL)
        self.assertEqual(recs["etk800_seat_FL"]["source_id"], "etk800_seat_FR")

    def test_lhd_token_is_not_a_side_pair(self) -> None:
        # bx-style handedness variants must not pair _lhd with _rhd, but the
        # lone lhd mirror is still one-sided hardware worth mirroring.
        recs = recommend({"bx_mirror_int_lhd": 0.0, "bx_mirror_int_rhd": 0.0})
        self.assertEqual(recs["bx_mirror_int_lhd"]["mode"], core.MODE_MIRROR)
        self.assertEqual(recs["bx_mirror_int_lhd"]["source_id"], "")

    def test_side_pair_inside_rhd_variant_still_pairs(self) -> None:
        recs = recommend({"bx_mirror_L_rhd": 0.4, "bx_mirror_R_rhd": -0.4})
        self.assertEqual(recs["bx_mirror_L_rhd"]["mode"], core.MODE_MIRROR_STRUCTURAL)
        self.assertEqual(recs["bx_mirror_L_rhd"]["source_id"], "bx_mirror_R_rhd")

    def test_shiftlight_with_mirror_token_is_translate(self) -> None:
        recs = recommend({"shiftlight_multi_led_mirror": 0.0})
        self.assertEqual(recs["shiftlight_multi_led_mirror"]["mode"], core.MODE_TRANSLATE)

    def test_column_top_translates_but_column_body_mirrors(self) -> None:
        recs = recommend(
            {
                "sunburst2_steering_column_top": 0.4,
                "sunburst2_steering_column": 0.4,
                "sunburst2_steering_column_race_rack": 0.4,
            }
        )
        self.assertEqual(recs["sunburst2_steering_column_top"]["mode"], core.MODE_TRANSLATE)
        self.assertEqual(recs["sunburst2_steering_column"]["mode"], core.MODE_MIRROR)
        self.assertEqual(recs["sunburst2_steering_column_race_rack"]["mode"], core.MODE_MIRROR)

    def test_pedalbox_footplate_translates_but_standalone_footplate_mirrors(self) -> None:
        recs = recommend({"grp_padalbox_footplate": 0.4, "race_footplate": 0.4})
        self.assertEqual(recs["grp_padalbox_footplate"]["mode"], core.MODE_TRANSLATE)
        self.assertEqual(recs["race_footplate"]["mode"], core.MODE_MIRROR)


if __name__ == "__main__":
    unittest.main()
