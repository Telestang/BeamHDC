# Plate Sets and XP Vehicle Exports

This document records the implemented plate/export model.

## Trim outputs

One source trim remains one table row. Its `Build` value expands to zero, one, or two output configs:

| Build value | Outputs |
| --- | --- |
| Off | None |
| Converted | The opposite-handed `_rhd`/`_lhd` config |
| Plates Only | An unconverted `_plates` config |
| Both | Both configs above in the same archive |

All vehicle packages use `<source>_XP_conversion.zip`. Plate design, front plate part, and rear plate
part are independent per-trim settings shared by both outputs. This intentionally means LHD and RHD
outputs from one row use the same plate design. Each side's dropdown lists BeamNG's shared vanilla
physical plate meshes, independent of the source trim. The trim's current model-specific part is shown
first with `(default)` and `None` is available per side. BeamXP uses the closest plate JBeam placement
defined by the loaded vehicle model, clones it into the trim's active slot when necessary, and swaps in
the selected shared mesh and format. The ModernGL `Config` dropdown lists the source trim only once;
`Original layout` removes the hand-drive transforms but deliberately retains its physical plate choices.

## Reusable sets

Plate sets live at `<BeamXP data>/plates/<id>.json` with `{id, name, config}`. The slug ID is fixed and
the display name can change. A conversion plate binding is `{mode, setId, config}` where `config` is the
latest resolved snapshot. Modes are `off`, `custom`, and `set`; a trim also supports internal vehicle
inheritance and conversion-local references such as `Custom (sport_RS_M)`. Those references are live,
so several trims can share one model-specific custom definition without promoting it to the global library.

Set references are live at build time. A missing set falls back to its snapshot and adds a warning.
Set-based parts use `bhdc_plateset_<id>` in both vehicle XP builds and the universal plates mod, avoiding
duplicate designs when both mods are installed.

The library manager supports New, Duplicate, Rename, Delete, Edit, and a checked export to one
`BeamXP_plates.zip`. Font atlases always contain A-Z, 0-9, space, hyphen, and period so registrations
chosen by stock vehicles remain visible.

## Deliberate boundaries

- Registration patterns generate text for BeamXP-exported configs, not arbitrary stock configs.
- A rear colour different from the front requires an XP vehicle trim and its cloned rear part.
- Universal plate-set designs use the front colour for both sides.
