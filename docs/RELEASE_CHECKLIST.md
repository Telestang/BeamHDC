# Release Checklist

Use this before pushing a public release or posting download links.

## Repository

- Confirm `hand_drive_tool_settings.json` is not committed.
- Confirm `handedness_conversion_projects/` is not committed.
- Confirm generated converted mod zips and source vehicle files are not committed to the MIT-licensed repository.
- Run:

```powershell
python -m py_compile beamng_hand_drive_core.py beamng_hand_drive_tool.py blender_preview_backend.py beamng_transform_helpers.py mesh_preview.py model_preview.py
```

## Suggested GitHub Release Notes

```text
v0.2.3-alpha

Fixes mesh placement for vehicles where the same part appears in several
mutually exclusive fitments, and the false-positive Windows Defender flag
some users hit on the release exe.

Highlights:
- Parts that only ever appear in ONE trim (e.g. the D-Series gooseneck
  hitch, fitted by five mutually-exclusive parts at two different
  positions) are now placed correctly per trim, instead of averaged
  across every fitment that could ever hold the mesh
- Fixed the 3D preview and Blender preview rendering some vanilla
  flexbody meshes far from the vehicle (a DAE node-translation handling
  bug that mixed up vanilla and mod content); mod content keeps its
  existing correct placement
- The parts table's X/Y/Z columns now show each part's actual placed
  geometry centre for the trim being previewed, instead of a raw DAE
  pivot that read 0,0,0 for a number of parts
- The auto delta calculation now falls back to the steering column's
  rotation centre when the steering wheel mesh itself has no usable
  off-centre position
- Windows Defender may flag the release exe as Trojan:Script/Wacatac.B!ml;
  this is a known false positive for unsigned PyInstaller apps, documented
  in the README Status section. Releases are now built without UPX
  compression, which removes one common trigger for this class of flag

Previous release highlights:
- Fixed parts going missing on vehicles whose jbeam uses a stray comma
  before the part body ("part":, { ... }). This hid 39 parts on the
  Bluebuck alone (bumpers, doors, fenders, hood, headlights, mirrors,
  door panels, trunk) and also affects the Grand Marshal, Roamer and D-Series
- Fixed an "Unclosed [] block" error that stopped the D-Series loading
  entirely, caused by a commented-out slot row with an unbalanced quote
- Commented-out slot rows no longer pull in parts the game never loads
- Much faster first-time vehicle loads: the D-Series went from ~229s to
  ~21s, and other vehicles roughly halved. Cached loads were already
  instant and are unchanged
- Preview point sampling is now reproducible run to run
- Flip Tex: un-mirrors the texture on mirrored display screens so satnav
  and infotainment content keeps its left/right reading
- Smarter Recommend Modes, tuned against hand-verified conversions:
  steering wheels, screens (mirror + Flip Tex), one-sided seat/mirror
  hardware, and cleaner handling of lhd/rhd part names
- Mode dropdown on the parts table plus Q/W/E/R hotkeys (Skip / Mirror
  Aesthetic / Mirror Structural / Translate), hotkeys work straight from
  the 3D preview
- More robust steering-ref auto-detection (plain "steer" names, vehicle's
  own wheel preferred over shared-library wheels), and detection re-runs
  on load when a project has no reference set

Known limitations:
- Severe crash deformation of some converted interior visuals may not perfectly match a hand-authored conversion
- Some vehicles/mods need manual offsets or part mode tuning
- Blender preview is optional but recommended for detailed checking
```

## Suggested Forum/Reddit Text

```text
I made an experimental BeamNG hand-drive conversion tool.

It loads a vehicle zip, lets you select variants/trims, choose which interior meshes to translate, mirror aesthetically, or mirror structurally, preview the result, and build a converted mod zip. It supports both LHD to RHD and RHD to LHD, including batch conversion for multiple configs.

The source vehicle physics stay intact. The tool moves the visual representation of driver-area parts rather than rebuilding the physical JBeam for every interior structure. Translated and aesthetic-mirrored parts can therefore show visual deformation from their original physical side in severe impacts; structural mirroring is intended for paired parts where deformation should behave on par with the original base vehicle.

This is an early alpha, but it already works on several tested vehicles. Blender preview is optional but recommended.
```

## Asset Sharing Note

Keep the GitHub repository to the tool code and conversion configs. Do not put source vehicle files or generated converted vehicle zips under the repository's MIT license.

## Windows Exe Build

Build the non-technical-user release archive with:

```powershell
.\packaging\build_windows.ps1 -Version 0.2.3-alpha
```

Confirm the generated archive contains:

- `BeamXP/BeamXP.exe`
- `BeamXP/README.md`
- `BeamXP/LICENSE`

Confirm the standalone exe can build a conversion and install the generated mod zip into the configured BeamNG mods folder. It should also work when copied out of the release folder without README/LICENSE.

Upload the generated `release/BeamXP-<version>-windows.zip` to GitHub Releases. Keep the source code in the repository for users who prefer running the tool with Python.

