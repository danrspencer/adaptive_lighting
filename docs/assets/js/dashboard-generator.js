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

// Phase/Sticky and the five schedule times stay plain tile cards - Phase
// gets the select-options feature so it's an inline dropdown rather than
// tap-to-open; a time entity has no stock inline-editable feature at
// all, so tile (tap opens the time picker) is already the best available
// option for it.
//
// The eight curve values and eight transition minutes are each a tile
// carrying the numeric-input feature (style: slider) - a first attempt
// used gauge cards instead, on the theory that seeing where today's
// value sits in a fixed range (brightness 0-255, colour temperature
// 1000-10000 Kelvin, transition 0-1440 minutes - see number.py's
// _CurveNumber) was the point of glancing at this section. Wrong in
// practice: a gauge is read-only in Home Assistant, tapping it only
// opens the more-info dialog - and this section is a control panel, not
// a readout, so losing the drag was a real regression. numeric-input
// restores it, and reads the entity's own configured min/max/step
// directly (confirmed against Home Assistant's own docs -
// https://www.home-assistant.io/dashboards/features/), so unlike the
// gauge version this generator carries no hardcoded ranges of its own to
// drift from number.py's if they ever change. style: slider overrides
// the entity's own mode: "box" more-info preference deliberately - fast
// drag-to-set on the dashboard and precise typed entry (still mode: box,
// via the entity's own more-info dialog opened another way) are two
// different, both still available, ways to set the same value.
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
  - type: tile
    entity: number.${slug}_morning_brightness
    name: Morning brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_morning_kelvin
    name: Morning colour temp
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_day_brightness
    name: Day brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_day_kelvin
    name: Day colour temp
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_evening_brightness
    name: Evening brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_evening_kelvin
    name: Evening colour temp
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_night_brightness
    name: Night brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_night_kelvin
    name: Night colour temp
    features:
      - type: numeric-input
        style: slider
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
  - type: tile
    entity: number.${slug}_morning_brightness_transition
    name: Morning brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_morning_kelvin_transition
    name: Morning colour
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_day_brightness_transition
    name: Day brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_day_kelvin_transition
    name: Day colour
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_evening_brightness_transition
    name: Evening brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_evening_kelvin_transition
    name: Evening colour
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_night_brightness_transition
    name: Night brightness
    features:
      - type: numeric-input
        style: slider
    grid_options:
      columns: 3
  - type: tile
    entity: number.${slug}_night_kelvin_transition
    name: Night colour
    features:
      - type: numeric-input
        style: slider
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
