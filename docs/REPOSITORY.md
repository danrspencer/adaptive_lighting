# Repository layout

Moved out of the main README since it's mostly of interest to contributors,
not to someone deciding whether to install this. See [../README.md](../README.md)
for the pitch and installation instructions.

```
custom_components/adaptive_lighting_helpers/
    __init__.py    registers the four services against real HA state
    coordinator.py shared schedule computation behind the sensors/select
                   below - one instance per sensor added via the
                   integration's "Add Sensor" flow
    sensor.py      day-phase/curve sensors (see docs/HELPERS.md)
    select.py      phase-override select (same doc)
    number.py      brightness/colour-temperature curve config, as
                   entities (same doc)
    time.py        schedule boundary times, as entities (same doc)
    switch.py      sticky-phase-override toggle, as an entity (same doc)
    curve.py       brightness/colour-temperature schedule + Kelvin -> RGB
    grouping.py    reachability, multiplier bucketing, tolerance checks,
                   manual-override protection, two-step/combined and
                   RGB-vs-colour-temp routing
    scenes.py      scene-coverage gap filling (apply a scene, then a
                   default for whatever it doesn't cover)
    manifest.json, config_flow.py, services.yaml, strings.json,
    translations/  standard HA integration/HACS scaffolding
    brand/icon.png, brand/icon@2x.png
                   the integration's icon (256/512, alpha) - HA reads
                   this directly from the integration's own folder
                   (since HA 2026.3.0), no external submission needed
    www/adaptive-lighting-curve-card.js
                   the dashboard card, served and auto-loaded by the
                   integration itself (see __init__.py's async_setup) -
                   ships and updates with the integration, no manual
                   Lovelace resource registration needed
    curve.py, grouping.py, and scenes.py are pure Python, no Home
    Assistant dependency - testable directly, and usable from anywhere
    that wants the math without the HA service/sensor wrapper around
    it. __init__.py, coordinator.py, sensor.py, select.py, number.py,
    time.py, and switch.py are the only files that touch `hass`.

hacs.json
    HACS repository metadata for the integration.

brand/
    generate_icon.py  renders brand/icon.svg from the real curve module
                      (same pattern as the dashboard preview generators):
                      the icon is the day's actual brightness/colour
                      curve as bars. Design/authoring tooling only - the
                      PNGs HA actually reads live at
                      custom_components/adaptive_lighting_helpers/brand/
                      (rendered from icon.svg, not scripted yet)
    icon.svg          the icon's source of truth, regenerate with
                      generate_icon.py after changing the curve defaults

blueprints/automation/danspencer/adaptive_lighting.yaml
    The automation blueprint: triggers, conditions, target resolution,
    and the action sequence (which service to call, with what target).
    Deliberately named differently from any prior "Adaptive Lighting
    Unified" blueprint so the two can run side by side while rooms are
    migrated over individually, rather than one replacing the other
    in place.

dashboard/
    house-settings-card.yaml   curve card config to add to a view
    adaptive-lighting-section.yaml
                                 fuller section: curve card, phase
                                 override, and every schedule/curve
                                 config entity, laid out as tiles
    preview.html                renders the real card against synthetic
                                 data, no Home Assistant instance needed
    generate_preview_data.py    generates that synthetic data
    render_preview_svg.py       renders the screenshot above as a
                                 standalone SVG

tests/
    pytest suite for curve.py, grouping.py, and scenes.py.

docs/
    HELPERS.md     full service/sensor reference for the integration
    BLUEPRINT.md   full feature/input reference for the blueprint
    REPOSITORY.md  this file
```

Triggers, conditions, and target resolution stay in the blueprint; Home Assistant `condition:` blocks can't call
a service, so anything a condition depends on has to remain template-based. Multiplier bucketing, tolerance
checks, and transition routing are implemented in the integration and unit tested. See `CLAUDE.md` for further
implementation notes, including the (fairly involved) history of getting a custom integration to load correctly
at all.
