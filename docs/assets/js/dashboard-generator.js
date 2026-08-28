/**
 * Drives the docs site's dashboard section generator (docs/dashboard.html).
 *
 * Every schedule sensor produces the same 25-entity dashboard section -
 * only the slug changes. This just does the substitution client-side and
 * hands back copy-pasteable YAML; it doesn't talk to a live Home
 * Assistant instance at all (unlike playground.js, which renders the
 * real card against synthetic data).
 */

const slugInput = document.getElementById('dgen-slug');
const titleInput = document.getElementById('dgen-title');
const output = document.getElementById('dgen-yaml');
const copyButton = document.getElementById('dgen-copy');
const status = document.getElementById('dgen-status');

// Matches slugify() in custom_components/flare/__init__.py closely enough
// for this purpose: lowercase, spaces/dashes to underscores, drop
// anything else. Not required to be byte-identical - this only has to
// produce entity IDs that look right, the source of truth for what a
// schedule is actually named is the sensor itself.
function slugify(raw) {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')
    .replace(/[^a-z0-9_]/g, '');
}

function titleCase(slug) {
  return slug
    .split('_')
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ');
}

// Phase/Sticky and the five schedule times stay tile cards - neither a
// select nor a time value has a meaningful "how full is this" reading,
// which is what a gauge is for. Phase gets the select-options feature so
// it's an inline dropdown rather than tap-to-open; a time entity has no
// stock inline-editable feature at all, so tile (tap opens the time
// picker) is already the best available option for it.
//
// The eight curve values and eight transition minutes are exactly what
// a gauge is for: each is a single number in a fixed, known range
// (brightness 0-255, colour temperature 1000-10000 Kelvin, transition
// 0-1440 minutes - see number.py's _CurveNumber), and seeing where today's
// value sits in that range at a glance is the actual point of glancing at
// this section. These entities are also configured mode: "box" rather
// than a slider (precise Kelvin/minute entry doesn't suit dragging), so a
// gauge's tap-through-to-more-info editing matches how they're meant to
// be set anyway - a tile's inline slider feature would fight that.
//
// grid_options.columns is out of the SECTION's own 12-column grid (see
// CLAUDE.md lesson 15) - 12 is one full-width row per card, which is what
// the original tile-only layout used throughout and why it read as a
// long single-column list despite the section itself being full-width.
// Smaller values here (6/4/3) let several cards share a row instead.
function buildYaml(slug, title) {
  return `type: grid
column_span: 4
cards:
  - type: heading
    heading: ${title}
    heading_style: title
    icon: mdi:chart-bell-curve
    badges:
      - type: entity
        entity: select.${slug}_flare_phase
        show_state: true
        show_icon: true
  - type: custom:flare-curve-card
    sensor: ${slug}
    grid_options:
      columns: full
    title: ''
  - type: heading
    heading: Override
    heading_style: subtitle
  - type: tile
    entity: select.${slug}_flare_phase
    name: Phase
    features:
      - type: select-options
    grid_options:
      columns: 6
  - type: tile
    entity: switch.${slug}_sticky_phase_override
    name: Sticky
    grid_options:
      columns: 6
  - type: heading
    heading: Schedule
    heading_style: subtitle
  - type: tile
    entity: time.${slug}_morning_time
    name: Morning
    grid_options:
      columns: 4
  - type: tile
    entity: time.${slug}_day_time
    name: Day
    grid_options:
      columns: 4
  - type: tile
    entity: time.${slug}_evening_earliest_time
    name: Evening (earliest)
    grid_options:
      columns: 4
  - type: tile
    entity: time.${slug}_evening_latest_time
    name: Evening (latest)
    grid_options:
      columns: 4
  - type: tile
    entity: time.${slug}_night_time
    name: Night
    grid_options:
      columns: 4
  - type: heading
    heading: Curve
    heading_style: subtitle
  - type: gauge
    entity: number.${slug}_morning_brightness
    name: Morning brightness
    min: 0
    max: 255
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_morning_kelvin
    name: Morning colour temp
    min: 1000
    max: 10000
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_day_brightness
    name: Day brightness
    min: 0
    max: 255
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_day_kelvin
    name: Day colour temp
    min: 1000
    max: 10000
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_evening_brightness
    name: Evening brightness
    min: 0
    max: 255
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_evening_kelvin
    name: Evening colour temp
    min: 1000
    max: 10000
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_night_brightness
    name: Night brightness
    min: 0
    max: 255
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_night_kelvin
    name: Night colour temp
    min: 1000
    max: 10000
    needle: true
    grid_options:
      columns: 3
  - type: heading
    heading: Transitions
    heading_style: subtitle
  - type: markdown
    text_only: true
    grid_options:
      columns: full
    content: >-
      How long before each phase ends to start easing into the next one, in
      minutes. 0 is a hard cut. Values are clamped to the phase, so anything
      longer than the phase itself means "ease across the whole phase".
  - type: gauge
    entity: number.${slug}_morning_brightness_transition
    name: Morning brightness
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_morning_kelvin_transition
    name: Morning colour
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_day_brightness_transition
    name: Day brightness
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_day_kelvin_transition
    name: Day colour
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_evening_brightness_transition
    name: Evening brightness
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_evening_kelvin_transition
    name: Evening colour
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_night_brightness_transition
    name: Night brightness
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
  - type: gauge
    entity: number.${slug}_night_kelvin_transition
    name: Night colour
    min: 0
    max: 1440
    needle: true
    grid_options:
      columns: 3
`;
}

// The title field tracks the slug field automatically until someone
// actually types into it themselves - after that their own text wins,
// same "auto until touched" pattern a lot of slug/name pairs use.
let titleTouched = false;
titleInput.addEventListener('input', () => {
  titleTouched = true;
  render();
});

// Matches the placeholder text shown in both empty inputs, so the
// output box always shows a coherent worked example - not a generic
// stand-in the placeholders never mention - until something real is typed.
const EXAMPLE_SLUG = 'downstairs';

function render() {
  const slug = slugify(slugInput.value) || EXAMPLE_SLUG;
  if (!titleTouched) {
    titleInput.value = titleCase(slug);
  }
  const title = titleInput.value.trim() || titleCase(slug);
  output.textContent = buildYaml(slug, title);
}

slugInput.addEventListener('input', render);

copyButton.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(output.textContent);
    status.textContent = 'Copied.';
  } catch {
    status.textContent = "Couldn't copy automatically - select the text above and copy it by hand.";
  }
  setTimeout(() => {
    status.textContent = '';
  }, 2500);
});

render();
