# BeamNG Hand Drive Converter (BeamHDC)

Convert any BeamNG.drive vehicle — vanilla or mod — between left-hand drive and right-hand drive.

**[Download BeamHDC 0.1.1-alpha](https://github.com/Telestang/BeamHDC/raw/main/release/BeamHDC-0.1.1-alpha-windows.zip)** — extract it anywhere and run the exe.

![A converted RHD Sunburst next to its stock LHD counterpart](Screenshots/sunburst_sunset_pair.jpg)

The aim is practical gameplay use: convert a car into the driver's preferred handedness with as little manual work as possible. The tool keeps the source vehicle physics, drivetrain, suspension, tires, handling, and damage model intact. It changes the visible driver environment by mirroring, translating, and remapping selected meshes and visual JBeam references.

| Stock LHD | Converted RHD |
| --- | --- |
| ![Stock LHD interior](Screenshots/sunburst2_lhd.jpg) | ![Converted RHD interior](Screenshots/sunburst2_rhd.jpg) |

## Status

The tool is new. It has been working well in my own testing, but there may be issues I am not aware of yet. If you find something, please take the time to report it.

## What It Does

- Converts in both directions: LHD to RHD and RHD to LHD.
- Lists `.pc` variants/trims and batch converts all selected variants in one build.
- Can export a converted trim, a Plates Only copy of the original trim, or both in one vehicle mod.
- Keeps reusable licence-plate sets in a global library and can export them as one universal plates mod.
- Selects front and rear plate meshes independently from BeamNG's shared vanilla physical-plate library; each trim's stock part is labelled `(default)` and `None` is available per side.
- Shows a live in-app 3D preview of the conversion that updates as you work.
- Builds one output mod zip containing all selected XP trim outputs and installs it into your BeamNG mods folder.
- Lets each part be set to `Skip`, `Translate`, `Mirror Aesthetic`, or `Mirror Structural`.
- Detects steering side where possible.
- Estimates the translate distance from a steering-wheel reference part.
- Allows per-part translate offsets.
- Converts internal camera positions automatically.
- Filters the part list to parts used by the selected variants.
- Loads any BeamNG vehicle `.zip`, including zips with multiple vehicle IDs.
- Optionally opens a Blender preview.

## Quick Start

1. [Download the release zip](https://github.com/Telestang/BeamHDC/raw/main/release/BeamHDC-0.1.1-alpha-windows.zip), extract it, and run `BeamNG Hand Drive Converter.exe` (or run from source — see Requirements).
2. Select a source BeamNG vehicle `.zip`.
3. If prompted, choose the vehicle model ID.
4. Select the variants/trims you want to convert.
5. Set part modes:
   - `Translate` for steering wheels, pedals, gauges, stalks, screens, and other driver-specific interior items.
   - `Mirror Aesthetic` for parts that only need visual mirroring.
   - `Mirror Structural` for paired parts where you want the opposite-side mesh on the existing structure, such as door cards or mirrors.
6. Mark the steering wheel part as `Steering Ref` if automatic delta detection needs help.
7. Use the in-app preview or Blender preview to inspect alignment.
8. When the preview looks right, set the BeamNG mods folder and click `Build + Install`.

Enjoying driving from the other side? Star the repo to help other people find it, or support development on [Ko-fi](https://ko-fi.com/telestang).

## Requirements

### Windows Build (Recommended)

This is the intended way to run the tool. Download the release zip, extract it anywhere, and run:

```text
BeamNG Hand Drive Converter.exe
```

No Python install is required. Blender is optional and external; set the path to `blender.exe` inside the tool if you want Blender previews.

The tool itself does not need to live in the BeamNG `mods` folder. Configure the BeamNG mods folder inside the app so `Build + Install` can copy generated conversion zips there.

### Running From Source

- Windows
- Python 3.11 or newer recommended
- Tkinter, normally included with the standard Windows Python installer
- BeamNG.drive source vehicle zips
- Optional: Blender 4.2+ for the Blender preview

Install Python packages:

```powershell
pip install -r requirements.txt
```

`requirements.txt` covers the in-app preview (numpy, moderngl) and preview image handling (Pillow). PyInstaller is only installed by the packaging script if you build the Windows exe yourself.

The tool can still build conversions without Blender configured.

### Linux

A user has reported the Windows build running on Linux under Wine without issue.

BeamNG itself runs through Proton on Linux, so point the mods folder at the Proton prefix, for example:

```text
~/.steam/steam/steamapps/compatdata/284160/pfx/drive_c/users/steamuser/AppData/Local/BeamNG.drive/current/mods
```

Running from source natively should also work - nothing in the tool is Windows-only - but I haven't tested it. If you try it, let me know how it goes.

## In-App Preview

![The tool with a conversion in progress: parts coloured by mode in the live 3D preview](Screenshots/sunburst2_tool.png)

The main window includes a live 3D preview of the selected `Config` trim. Feedback is instant: changing a part mode, offset, plate mesh, or any other conversion setting updates the preview immediately, with no build step.

- Left-drag orbits, right- or middle-drag pans, and the mouse wheel zooms.
- Click a part in the viewport to select it in the parts table.
- `H` (or `Shift+H`) hides/unhides the selected parts.
- The `Opacity` slider makes the vehicle see-through so you can check buried interior parts.
- The `Original layout` checkbox removes the hand-drive mesh/prop transforms while retaining the selected replacement plates.
- Parts are coloured by mode: grey for non-transformed parts, blue for `Translate`, orange for `Mirror Aesthetic`, and pink for `Mirror Structural`.

![Lowering the opacity reveals the converted interior through the bodywork](Screenshots/sunburst2_transparent_tool.png)

The in-app preview shows one trim/variant at a time.

## Blender Preview

The Blender preview is optional. Use it when you want to inspect the complete generated trim with full Blender tooling; the in-app preview is the quicker feedback loop for day-to-day tuning.

The preview:

- Builds the current unpacked output first.
- Uses the selected `Config` entry.
- Imports the final resolved vehicle for that output, including generated converted meshes and unchanged context meshes.
- Does not require the BeamNG Blender JBeam Editor add-on.
- Opens as a new unsaved Blender instance; nothing is written to disk unless you save it yourself from Blender.

If Blender is not configured, zip generation still works.

For fine offset tuning, select the part that needs adjustment in a Blender preview, move it on Blender's X axis until it lines up, then copy that X movement into the tool as a manual global or per-part offset.

## Part Modes

### Translate

Moves the visual mesh laterally without mirroring it. Use this for parts that should stay oriented the same relative to the driver:

- Steering wheel
- Gauge cluster
- Needles and screens
- Pedals
- Stalks
- Driver-specific controls

For translated props, the tool keeps the original `idRef`, `idX`, `idY`, rotations, and animation values, and adds `baseTranslationGlobal` so animated props rotate around the translated visual position.

### Mirror Aesthetic

Mirrors the generated mesh visually. For large symmetric interior parts such as dashboards, centre consoles, and headliners, this is a non-issue: the result deforms on par with the stock vehicle.

Where mirroring creates a significant visual asymmetry — a vehicle with only a driver-side wing mirror, a race car with a single front seat — the deformation will follow the original physical side and won't look right in severe crashes. That is a trade-off of using this tool; use `Mirror Aesthetic` where you need it.

### Mirror Structural

Swaps an opposite-side mirrored mesh onto the existing source-side JBeam structure. This is useful for paired parts like door cards or mirrors where you want the visual side to change but still deform with the existing door/mirror structure.

## Physics And Deformation Notes

The tool does not move the physical JBeam structure for driver controls. The physical steering wheel, pedals, handbrake, and similar interior structures remain where they are in the source vehicle. The generated mod moves their visual representation.

Visual deformation is still driven by the source vehicle's physical deformation. For `Translate` parts, this can mean severe crash damage deforms the visual part according to its original physical side. For example, a heavy left-side impact that would deform the driver's side of a LHD car may visibly affect a translated RHD driver's visual part even though that visual part is now on the right.

`Mirror Aesthetic` on symmetric parts such as headliners, dashboards, and centre consoles deforms on par with the original base vehicle. The original-physical-side caveat only applies when `Mirror Aesthetic` results in visual asymmetry — for example, a vehicle that only has a driver-side wing mirror, or a race car with a single front seat.

`Mirror Structural` swaps an opposite-side mesh onto an existing opposite-side structure, so deformation behaves on par with the original base vehicle.

## Output

Projects are saved under:

```text
%LOCALAPPDATA%/BeamHDC/handedness_conversion_projects/<projectName>/
```

The app settings file is saved beside the projects under `%LOCALAPPDATA%/BeamHDC/`. This keeps user work stable even if the app folder or exe is replaced during an update.

Each project contains:

- `conversion.json`: saved tool settings for that source zip name and vehicle ID
- `unpacked_output/`: generated mod folder
- `build/`: generated mod zip
- `blender_preview/`: Blender preview working files (payloads and extracted DAE caches), if used

The configured BeamNG mods folder is only used as the install target for generated conversion zips.

Vehicle builds use the filename `<source>_XP_conversion.zip`. Each trim's `Build` cell can be `Off`,
`Converted`, `Plates Only`, or `Both`, so a converted and a Plates Only copy of the same source trim can
live in that one archive. The in-app `Config` dropdown still lists that source trim only once; `Original
layout` changes the previewed transform state without changing its selected plates.

Reusable plate sets are stored separately under `%LOCALAPPDATA%/BeamHDC/plates/`. Renaming a set is
safe because projects reference its fixed ID. Builds resolve the latest set contents and embed a
snapshot; if a referenced set is later deleted, the snapshot is used with a build warning. The plate
library can export selected sets to `BeamHDC_plates.zip` for use in the parts menu on stock vehicles.

Model-local custom designs are labelled `Custom (<vehicle ID>)` and `Custom (<config name>)`. Once a
trim custom exists, other trims can select it and share the same live definition without adding it to
the global library. A trim's converted and Plates Only outputs deliberately share one plate selection;
BeamNG's parts menu can still switch either vehicle to any generated custom or library design in game.

On stock vehicles BeamNG controls the registration text; a BeamHDC pattern such as `@@## @@@` only
generates registrations for exported trim configs. Different front/rear background colours require a
converted or plates-only trim because BeamHDC must select a cloned rear plate part. Universal designs
use the front colour on both sides.

The output mod zip also embeds a copy of the conversion settings at:

```text
handedness_conversion/conversion.json
```

`projectName` is normally the vehicle ID when the zip name matches it, such as `sunburst2`. If a zip contains a differently named vehicle folder, the project name includes both the zip name and vehicle ID.

## Example Configs

The `examples/conversion_configs/` folder contains example conversion settings:

- `sunburst2_batch_conversion.json`: Hirochi Sunburst, all 39 variants LHD to RHD
- `bx_batch_conversion.json`: Ibishu 200BX, all 36 variants RHD to LHD

These are settings examples, not source vehicles. To use one:

1. Load your own matching source vehicle zip.
2. Use `Import Config`.
3. Select the example config.
4. The tool imports only matching variants and part names.

Converted vehicle zips are not included because this repository is MIT licensed and cannot include BeamNG source vehicle files under that license. The example configs use vanilla BeamNG vehicles and contain settings only.

## Known Limitations

- Some vehicles use sloppy, inconsistent JBeam syntax. The parser handles several known quirks, but more may appear.
- Some community mods have off-center geometry or inconsistent object origins. Use manual global or per-part offsets.
- Some animated props may need vehicle-specific attention.
- Texture paths in Blender preview may not resolve exactly like BeamNG's material system.
- Wheel-attached meshes (road wheels, hubcaps, tires) may not be positioned correctly in previews. The game places them at runtime on wheel node groups generated by the wheel system, which the previews do not model. Generated output zips are unaffected; the game positions them correctly.
- Severe crash deformation of translated or asymmetrically mirrored interior visuals may not perfectly match a hand-authored conversion.
- In the first-person driver camera, the "lean head out of the window" movement is clamped on right-hand-drive vehicles: the head barely exits the window when looking toward the driver's side. This is a BeamNG engine bug (a frame mismatch in the driver camera's window-margin calculation in `lua/ge/extensions/core/cameraModes/driver.lua`), not a conversion defect — official RHD vehicles such as the vanilla 200BX are affected identically, and it has been [reported to BeamNG along with a fix](https://www.beamng.com/threads/rhd-driver-camera-bug.110306/). Converted cameras match the official RHD camera setup exactly, so please don't report this one here.

## Reporting Issues

Open a GitHub issue with three things:

- The source vehicle zip (or where to get it)
- The conversion settings: your project `conversion.json`, or the `handedness_conversion/conversion.json` embedded in the built zip
- A description of what is going wrong

With the zip and the config file, the attempted conversion can be reproduced exactly in the app via `Import Config`.

## Support

If this tool saved you from doing a conversion by hand, consider [buying me a coffee on Ko-fi](https://ko-fi.com/telestang). It keeps the project going.

Starring the repo helps too - it's free and it makes the tool easier for others to find.

## License

Tool code is MIT licensed. Generated output zips are not automatically MIT licensed; they may include or derive from the source vehicle's assets and remain subject to the source asset licenses.
