from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import beamng_hand_drive_core as core
import plate_generator


class PlateXpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous_data_dir = os.environ.get("BEAMHDC_DATA_DIR")
        os.environ["BEAMHDC_DATA_DIR"] = str(self.root / "data")

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("BEAMHDC_DATA_DIR", None)
        else:
            os.environ["BEAMHDC_DATA_DIR"] = self.previous_data_dir

    def _context(self, *, with_plate_parts: bool = False) -> core.VehicleContext:
        source = self.root / "test.zip"
        pc = {
            "mainPartName": "test",
            "parts": {
                "test_licenseplate_F": "plate_f_2",
                "test_licenseplate_R": "plate_r_wide",
            } if with_plate_parts else {},
            "licenseName": "STOCK",
        }
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("vehicles/test/base.pc", json.dumps(pc))

        part_index: dict[str, tuple[str, str]] = {}
        if with_plate_parts:
            main = {
                "slotType": "main",
                "slots": [
                    ["type", "default", "description"],
                    ["test_licenseplate_F", "plate_f_2", "Front"],
                    ["test_licenseplate_R", "plate_r_wide", "Rear"],
                ],
            }

            def plate_body(part_id: str, slot: str, fmt: str, name: str, mesh: str | None = None) -> str:
                body = json.dumps({
                    "information": {"name": name},
                    "slotType": slot,
                    "licenseplateFormat": fmt,
                    "flexbodies": [
                        ["mesh", "[group]:", "nonFlexMaterials"],
                        [mesh or ("licenseplate" if fmt == "30-15" else "licenseplate-52-11"), [], []],
                    ],
                })
                return f'"{part_id}": {body}'

            part_index = {
                "test": (json.dumps(main), "test.jbeam"),
                "plate_f_2": (plate_body("plate_f_2", "test_licenseplate_F", "30-15", "Front US Plate"), "plates.jbeam"),
                "plate_f_wide": (plate_body("plate_f_wide", "test_licenseplate_F", "52-11", "Front EU Plate"), "plates.jbeam"),
                "plate_f_alt_wide": (
                    plate_body(
                        "plate_f_alt_wide",
                        "test_licenseplate_F_alt",
                        "52-11",
                        "Front EU Curved Plate",
                        "licenseplate-52-11-r0_5",
                    ),
                    "plates_alt.jbeam",
                ),
                "plate_r_2": (plate_body("plate_r_2", "test_licenseplate_R", "30-15", "Rear US Plate"), "plates.jbeam"),
                "plate_r_wide": (plate_body("plate_r_wide", "test_licenseplate_R", "52-11", "Rear EU Plate"), "plates.jbeam"),
            }

        return core.VehicleContext(
            source,
            "test",
            "vehicles/test",
            [],
            {"base": core.VariantInfo("base", "vehicles/test/base.pc", None, "Base")},
            {},
            {},
            {},
            {},
            self.root / "project",
            part_body_index=part_index,
        )

    def test_legacy_selected_trim_migrates_to_converted(self) -> None:
        context = self._context()
        conversion = core.merge_with_current_inventory(context, {
            "variants": {"base": {"selected": True, "sourceHandOverride": core.HAND_LHD}},
            "plate": {"enabled": False},
        })
        self.assertEqual(conversion["variants"]["base"]["build"], core.BUILD_CONVERTED)
        self.assertEqual(conversion["plate"]["mode"], plate_generator.PLATE_MODE_OFF)

    def test_detected_stock_hand_is_cached_across_context_sessions(self) -> None:
        context = self._context()
        conversion = core.base_conversion_config(context)
        with patch.object(core, "detect_hand_for_variant", return_value=core.HAND_LHD) as detect:
            hands = core.detect_hands_for_variants(context, conversion)
        self.assertEqual(hands, {"base": core.HAND_LHD})
        detect.assert_called_once_with(context, conversion, "base")
        self.assertTrue(core.variant_hands_cache_path(context).is_file())

        context.variant_hands_cache = {}
        with patch.object(core, "detect_hand_for_variant", side_effect=AssertionError("cache miss")):
            cached = core.detect_hands_for_variants(context, conversion)
        self.assertEqual(cached, {"base": core.HAND_LHD})

        conversion["parts"]["steering_ref"] = {"steeringRef": True}
        with patch.object(core, "detect_hand_for_variant", return_value=core.HAND_RHD) as detect_changed:
            changed = core.detect_hands_for_variants(context, conversion)
        self.assertEqual(changed, {"base": core.HAND_RHD})
        detect_changed.assert_called_once_with(context, conversion, "base")

    def test_live_set_reference_keeps_deleted_snapshot(self) -> None:
        config = plate_generator.default_plate_config()
        config["eu"]["pattern"] = "SET ##"
        plate_generator.save_plate_set({"id": "set-one", "name": "Set One", "config": config})
        conversion = {
            "plate": {"mode": "set", "setId": "set-one", "config": plate_generator.default_plate_config()},
            "variants": {"base": {"plate": {"mode": "general"}}},
        }
        resolved, set_id = plate_generator.effective_plate_selection(conversion, "base")
        self.assertEqual(set_id, "set-one")
        self.assertEqual(resolved["eu"]["pattern"], "SET ##")

        plate_generator.delete_plate_set("set-one")
        warnings: list[str] = []
        fallback, _set_id = plate_generator.effective_plate_selection(conversion, "base", warnings=warnings)
        self.assertEqual(fallback["eu"]["pattern"], "SET ##")
        self.assertTrue(warnings)

    def test_trim_custom_reference_is_live_and_keeps_a_snapshot(self) -> None:
        source_config = plate_generator.default_plate_config()
        source_config["eu"]["pattern"] = "SPORT ##"
        conversion = {
            "plate": plate_generator.default_plate_binding(),
            "variants": {
                "sport_RS_M": {
                    "plate": {
                        "mode": "custom",
                        "sourceConfig": "sport_RS_M",
                        "customDefined": True,
                        "config": source_config,
                    },
                },
                "sport_RS_DCT": {
                    "plate": {
                        "mode": "trim",
                        "sourceConfig": "sport_RS_M",
                        "config": plate_generator.default_plate_config(),
                    },
                },
            },
        }
        resolved, set_id = plate_generator.effective_plate_selection(conversion, "sport_RS_DCT")
        self.assertIsNone(set_id)
        self.assertEqual(resolved["eu"]["pattern"], "SPORT ##")

        conversion["variants"]["sport_RS_M"]["plate"]["customConfig"]["eu"]["pattern"] = "UPDATED ##"
        updated, _set_id = plate_generator.effective_plate_selection(conversion, "sport_RS_DCT")
        self.assertEqual(updated["eu"]["pattern"], "UPDATED ##")

        del conversion["variants"]["sport_RS_M"]
        warnings: list[str] = []
        fallback, _set_id = plate_generator.effective_plate_selection(
            conversion,
            "sport_RS_DCT",
            warnings=warnings,
        )
        self.assertEqual(fallback["eu"]["pattern"], "UPDATED ##")
        self.assertTrue(warnings)

    def test_inline_design_is_named_beamhdc_custom(self) -> None:
        output = type("Design", (), {
            "design_json_rel": "vehicles/common/licenseplates/test/licensePlate.json",
        })()
        body = json.loads(plate_generator._design_part_body(output, "EU", custom=True))
        self.assertEqual(body["information"]["name"], "BeamHDC Custom")

    def test_both_expands_to_two_outputs_in_one_xp_package(self) -> None:
        context = self._context()
        conversion = core.base_conversion_config(context)
        settings = conversion["variants"]["base"]
        settings["sourceHandOverride"] = core.HAND_LHD
        core.set_variant_build_mode(settings, core.BUILD_BOTH)
        plans, skipped = core.selected_output_plans(context, conversion)
        self.assertFalse(skipped)
        self.assertEqual([plan["output"] for plan in plans], ["base_rhd", "base_plates"])
        self.assertEqual(core.package_name_for_context(context), "test_XP_conversion.zip")

    def test_rhd_trim_can_build_only_replacement_plates_without_an_lhd_output(self) -> None:
        context = self._context()
        conversion = core.base_conversion_config(context)
        settings = conversion["variants"]["base"]
        settings["sourceHandOverride"] = core.HAND_RHD
        core.set_variant_build_mode(settings, core.BUILD_ORIGINAL)
        plans, skipped = core.selected_output_plans(context, conversion)
        self.assertFalse(skipped)
        self.assertEqual(plans, [{
            "source": "base",
            "kind": core.BUILD_ORIGINAL,
            "targetHand": None,
            "output": "base_plates",
        }])

    def test_original_plate_build_changes_each_side_independently(self) -> None:
        context = self._context(with_plate_parts=True)
        front_values = plate_generator.plate_part_options_for_config(context, "base", "front")
        rear_values = plate_generator.plate_part_options_for_config(context, "base", "rear")
        self.assertEqual(front_values[0], "auto")
        self.assertIn("mesh:licenseplate-52-11", front_values)
        self.assertEqual(front_values[-1], "none")
        self.assertEqual(rear_values[0], "auto")
        self.assertIn("mesh:licenseplate", rear_values)
        self.assertEqual(rear_values[-1], "none")
        front_choices = plate_generator.plate_part_choices_for_config(context, "base", "front")
        self.assertEqual(front_choices[0].label, "US/JP (default)")
        conversion = core.base_conversion_config(context)
        settings = conversion["variants"]["base"]
        core.set_variant_build_mode(settings, core.BUILD_ORIGINAL)
        settings["frontPlate"] = "mesh:licenseplate-52-11"
        settings["rearPlate"] = plate_generator.PLATE_PART_NONE
        config = plate_generator.default_plate_config()
        config["enabled"] = True
        conversion["plate"] = {"mode": "custom", "setId": "", "config": config}

        preview_pc, _preview_aliases = plate_generator.preview_pc_with_plate_parts(context, conversion, "base")
        self.assertEqual(preview_pc["parts"]["test_licenseplate_F"], "plate_f_wide")
        self.assertEqual(preview_pc["parts"]["test_licenseplate_R"], "")

        result = core.build_batch(context, conversion, write_zip=True)
        generated_path = result.unpacked_dir / "vehicles/test/base_plates.pc"
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        self.assertEqual(generated["parts"]["test_licenseplate_F"], "plate_f_wide")
        self.assertEqual(generated["parts"]["test_licenseplate_R"], "")
        self.assertEqual(result.generated_configs, ["base_plates"])
        self.assertEqual(result.package_zip.name, "test_XP_conversion.zip")
        generated_parts = core.parse_beamng_json(
            (generated_path.parent / "jbeam/bhdc_licenseplates.jbeam").read_text(encoding="utf-8"),
            label="bhdc_licenseplates.jbeam",
        )
        custom_parts = [part for part in generated_parts.values() if isinstance(part, dict)]
        self.assertEqual(custom_parts[0]["information"]["name"], "BeamHDC Custom")

    def test_plate_part_from_another_model_slot_is_cloned_into_the_trim_slot(self) -> None:
        context = self._context(with_plate_parts=True)
        conversion = core.base_conversion_config(context)
        settings = conversion["variants"]["base"]
        core.set_variant_build_mode(settings, core.BUILD_ORIGINAL)
        settings["frontPlate"] = "mesh:licenseplate-52-11-r0_5"

        preview_pc, preview_aliases = plate_generator.preview_pc_with_plate_parts(context, conversion, "base")
        preview_selected = preview_pc["parts"]["test_licenseplate_F"]
        self.assertTrue(preview_selected.startswith("bhdc_plate_plate_f_alt_wide_"))
        self.assertIn(preview_selected, preview_aliases)

        result = core.build_batch(context, conversion, write_zip=False)
        generated_path = result.unpacked_dir / "vehicles/test/base_plates.pc"
        generated = json.loads(generated_path.read_text(encoding="utf-8"))
        selected = generated["parts"]["test_licenseplate_F"]
        self.assertTrue(selected.startswith("bhdc_plate_plate_f_alt_wide_"))

        generated_parts = core.parse_beamng_json(
            (generated_path.parent / "jbeam/bhdc_licenseplates.jbeam").read_text(encoding="utf-8"),
            label="bhdc_licenseplates.jbeam",
        )
        cloned = generated_parts[selected]
        self.assertEqual(cloned["slotType"], "test_licenseplate_F")
        self.assertEqual(cloned["flexbodies"][1][0], "licenseplate-52-11-r0_5")


if __name__ == "__main__":
    unittest.main()
