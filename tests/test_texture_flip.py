from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

import beamng_hand_drive_core as core
import beamng_transform_helpers as th


def build_geometry(uv_values: str) -> ET.Element:
    xml = f"""
    <geometry xmlns="{th.NS['c']}" id="Mesh_077-mesh" name="screen">
      <mesh>
        <source id="Mesh_077-mesh-positions">
          <float_array id="Mesh_077-mesh-positions-array" count="12">
            -0.5 0 0  0.5 0 0  0.5 0 1  -0.5 0 1
          </float_array>
          <technique_common>
            <accessor source="#Mesh_077-mesh-positions-array" count="4" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Mesh_077-mesh-map-0">
          <float_array id="Mesh_077-mesh-map-0-array" count="8">{uv_values}</float_array>
          <technique_common>
            <accessor source="#Mesh_077-mesh-map-0-array" count="4" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Mesh_077-mesh-vertices">
          <input semantic="POSITION" source="#Mesh_077-mesh-positions"/>
        </vertices>
        <triangles material="screen-material" count="2">
          <input semantic="VERTEX" source="#Mesh_077-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#Mesh_077-mesh-map-0" offset="1" set="0"/>
          <p>0 0 1 1 2 2 0 0 2 2 3 3</p>
        </triangles>
      </mesh>
    </geometry>
    """
    return ET.fromstring(xml)


def uv_pairs(geometry: ET.Element) -> tuple[list[float], list[float]]:
    mesh = geometry.find("c:mesh", th.NS)
    for source in mesh.findall("c:source", th.NS):
        accessor = source.find(".//c:accessor", th.NS)
        params = [p.get("name") for p in accessor.findall("c:param", th.NS)]
        if params[:2] == ["S", "T"]:
            values = [float(v) for v in source.find("c:float_array", th.NS).text.split()]
            return values[0::2], values[1::2]
    raise AssertionError("No TEXCOORD source found")


class TextureFlipTests(unittest.TestCase):
    def test_mirrored_geometry_leaves_uvs_alone_by_default(self) -> None:
        geometry = build_geometry("0.1 0.2  0.9 0.2  0.9 0.8  0.1 0.8")
        out = th.mirrored_geometry(geometry, "new")
        s, t = uv_pairs(out)
        self.assertEqual(s, [0.1, 0.9, 0.9, 0.1])
        self.assertEqual(t, [0.2, 0.2, 0.8, 0.8])

    def test_flip_texture_reflects_s_within_bounds(self) -> None:
        geometry = build_geometry("0.1 0.2  0.9 0.2  0.9 0.8  0.1 0.8")
        out = th.mirrored_geometry(geometry, "new", flip_texture=True)
        s, t = uv_pairs(out)
        for got, expected in zip(s, [0.9, 0.1, 0.1, 0.9]):
            self.assertAlmostEqual(got, expected)
        self.assertEqual(t, [0.2, 0.2, 0.8, 0.8])

    def test_flip_texture_preserves_offset_footprint(self) -> None:
        # UVs that live in a sub-region of an atlas (and outside 0..1, as the
        # stock etk800 screen does) must keep sampling the same region.
        geometry = build_geometry("-0.995 0.0  -0.4276 0.0  -0.4276 1.0  -0.995 1.0")
        out = th.mirrored_geometry(geometry, "new", flip_texture=True)
        s, _t = uv_pairs(out)
        self.assertAlmostEqual(min(s), -0.995)
        self.assertAlmostEqual(max(s), -0.4276)
        for got, expected in zip(s, [-0.4276, -0.995, -0.995, -0.4276]):
            self.assertAlmostEqual(got, expected)

    def test_flip_texture_still_mirrors_positions(self) -> None:
        geometry = build_geometry("0.0 0.0  1.0 0.0  1.0 1.0  0.0 1.0")
        out = th.mirrored_geometry(geometry, "new", flip_texture=True)
        mesh = out.find("c:mesh", th.NS)
        positions = next(
            source
            for source in mesh.findall("c:source", th.NS)
            if "positions" in source.get("id")
        )
        values = [float(v) for v in positions.find("c:float_array", th.NS).text.split()]
        self.assertEqual(values[0::3], [0.5, -0.5, -0.5, 0.5])

    def test_texture_flip_mesh_ids_requires_mirror_aesthetic_mode(self) -> None:
        # Mirror Structural is deliberately excluded: it swaps in the
        # opposite-side mesh, which already has its own correct mapping.
        conversion = {
            "parts": {
                "screen": {"mode": core.MODE_MIRROR, "textureFlip": True},
                "dash": {"mode": core.MODE_MIRROR, "textureFlip": False},
                "gauge": {"mode": core.MODE_TRANSLATE, "textureFlip": True},
                "panel": {"mode": core.MODE_MIRROR_STRUCTURAL, "textureFlip": True},
            }
        }
        modes = {
            "screen": core.MODE_MIRROR,
            "dash": core.MODE_MIRROR,
            "gauge": core.MODE_TRANSLATE,
            "panel": core.MODE_MIRROR_STRUCTURAL,
        }
        self.assertEqual(core.texture_flip_mesh_ids(conversion, modes), {"screen"})


if __name__ == "__main__":
    unittest.main()
