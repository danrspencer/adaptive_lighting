/**
 * Drives the docs site's interactive curve playground.
 *
 * The chart on this page is not a mock-up: it is the real
 * flare-curve-card.js that ships inside the integration,
 * given exactly the shape of state a live Home Assistant would give it
 * (a sensor whose state is the phase name and whose attributes carry
 * brightness/color_temp/the four boundaries/a 289-point `points` array).
 * Moving a slider recomputes those points with curve.js - the parity-
 * tested port of the integration's curve.py - and hands the card a fresh
 * `hass` object. So what you see here is what the card would draw in
 * your own dashboard for those settings.
 */

import {
  DEFAULT_CURVE_VALUES,
  DEFAULT_SCHEDULE_HOURS,
  buildPoints,
  eveningTsFor,
  phaseAt,
  targetsForPhase,
} from './curve.js';

// Resolved relative to THIS module (both files sit in assets/js/), not
// to the page - which is what a dynamic import() does, and what keeps
// this working whatever path the site is served under.
const CARD_URL = './flare-curve-card.js';

// Every control on the page. `key` doubles as the URL-hash key and the
// state key; `entity` is the Home Assistant entity the control stands in
// for, shown under each slider so the page doubles as a map from "this
// slider" to "this thing to change in HA".
const TIME_CONTROLS = [
  { key: 'morning', label: 'Morning Start', entity: 'time.<name>_morning_time' },
  { key: 'day', label: 'Day Start', entity: 'time.<name>_day_time' },
  { key: 'evening_earliest', label: 'Evening Earliest', entity: 'time.<name>_evening_earliest_time' },
  { key: 'evening_latest', label: 'Evening Latest', entity: 'time.<name>_evening_latest_time' },
  { key: 'night', label: 'Night Start', entity: 'time.<name>_night_time' },
  // Not configurable anywhere - it comes from sun.sun. Adjustable here
  // only so you can see how the Evening clamp reacts across the year.
  { key: 'sunset', label: 'Sunset', entity: 'sun.sun — not configurable, adjustable here only to try other times of year' },
];

const BRIGHTNESS_CONTROLS = [
  { key: 'morning_brightness', label: 'Morning Brightness', entity: 'number.<name>_morning_brightness' },
  { key: 'day_brightness', label: 'Day Brightness', entity: 'number.<name>_day_brightness' },
  { key: 'evening_brightness', label: 'Evening Brightness', entity: 'number.<name>_evening_brightness' },
  { key: 'night_brightness', label: 'Night Brightness', entity: 'number.<name>_night_brightness' },
];

const KELVIN_CONTROLS = [
  { key: 'morning_kelvin', label: 'Morning Colour Temperature', entity: 'number.<name>_morning_kelvin' },
  { key: 'day_kelvin', label: 'Day Colour Temperature', entity: 'number.<name>_day_kelvin' },
  { key: 'evening_kelvin', label: 'Evening Colour Temperature', entity: 'number.<name>_evening_kelvin' },
  { key: 'night_kelvin', label: 'Night Colour Temperature', entity: 'number.<name>_night_kelvin' },
];

// Named for the phase the transition runs *in* - it is that phase's exit,
// so "Day Colour Transition" is how long before Day ends to start easing
// to Evening's colour. 0 is a hard cut; anything longer than the phase
// clamps to it, which is how Day's default of 1440 reads as "slide all
// day".
const TRANSITION_CONTROLS = [
  { key: 'morning_brightness_transition', label: 'Morning Brightness Transition', entity: 'number.<name>_morning_brightness_transition' },
  { key: 'morning_kelvin_transition', label: 'Morning Colour Transition', entity: 'number.<name>_morning_kelvin_transition' },
  { key: 'day_brightness_transition', label: 'Day Brightness Transition', entity: 'number.<name>_day_brightness_transition' },
  { key: 'day_kelvin_transition', label: 'Day Colour Transition', entity: 'number.<name>_day_kelvin_transition' },
  { key: 'evening_brightness_transition', label: 'Evening Brightness Transition', entity: 'number.<name>_evening_brightness_transition' },
  { key: 'evening_kelvin_transition', label: 'Evening Colour Transition', entity: 'number.<name>_evening_kelvin_transition' },
  { key: 'night_brightness_transition', label: 'Night Brightness Transition', entity: 'number.<name>_night_brightness_transition' },
  { key: 'night_kelvin_transition', label: 'Night Colour Transition', entity: 'number.<name>_night_kelvin_transition' },
];

// Sunrise is display-only on the card (a dot on the top axis); it plays
// no part in the schedule, which is the point worth making on a page
// about a scheduler that deliberately doesn't track the sun.
const SUNRISE_HOUR = 6.4;

const DEFAULT_STATE = {
  ...DEFAULT_CURVE_VALUES,
  morning: DEFAULT_SCHEDULE_HOURS.morning * 60,
  day: DEFAULT_SCHEDULE_HOURS.day * 60,
  evening_earliest: DEFAULT_SCHEDULE_HOURS.evening_earliest * 60,
  evening_latest: DEFAULT_SCHEDULE_HOURS.evening_latest * 60,
  night: DEFAULT_SCHEDULE_HOURS.night * 60,
  sunset: 19 * 60 + 45,
  now: 20 * 60 + 30, // inside Evening on the defaults, so the page opens on the interesting part
};

// Named starting points. Each is a partial - anything unlisted keeps its
// default - so a preset reads as "what this scenario changes".
const PRESETS = {
  default: {},
  winter: { sunset: 16 * 60 + 10, now: 17 * 60 + 30 },
  midsummer: { sunset: 21 * 60 + 30, now: 21 * 60 },
  'late-riser': { morning: 8 * 60, day: 10 * 60, night: 23 * 60 + 30, now: 9 * 60 },
  'very-warm-night': { night_kelvin: 1800, evening_kelvin: 2400, night_brightness: 25, now: 23 * 60 },
};

const state = { ...DEFAULT_STATE };

/** A duration in minutes -> "30 min" / "1 h" / "1 h 30 min".
 *
 * Deliberately NOT fmtMinutes: that formats a time of day, wrapping at
 * 1440, so a full-day transition rendered as "00:00" - reading as zero,
 * the exact opposite of what it does. */
function fmtDuration(mins) {
  const m = Math.round(mins);
  if (m === 0) return 'off (hard cut)';
  const h = Math.floor(m / 60);
  const rest = m % 60;
  // Anything this long always clamps to the phase it runs in, so say so
  // rather than showing a number nothing can reach.
  if (m >= 1440) return 'whole phase';
  if (!h) return `${rest} min`;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** Minutes-since-midnight -> "HH:MM". */
function fmtMinutes(mins) {
  const m = ((Math.round(mins) % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
}

/** Today's local midnight, as unix seconds - the anchor every boundary
 *  below is an offset from, matching how coordinator.py builds them. */
function midnightTs() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

function boundariesFor(midnight) {
  const at = (mins) => midnight + mins * 60;
  return {
    morningTs: at(state.morning),
    dayStartTs: at(state.day),
    // The one derived boundary - see curve.js's eveningTsFor.
    eveningTs: eveningTsFor(at(state.sunset), at(state.evening_earliest), at(state.evening_latest)),
    nightTs: at(state.night),
  };
}

function curveValues() {
  const v = {};
  for (const key of Object.keys(DEFAULT_CURVE_VALUES)) v[key] = state[key];
  return v;
}

/* ---------------------------------------------------------------- *
 * The card's "now" marker reads Date.now() directly (it is a live
 * dashboard card; it has no notion of a simulated clock). Rather than
 * fork the card for the docs, shim Date.now on this page so the time-of-
 * day slider moves the marker. Only this page is affected, and only
 * Date.now() - `new Date(...)`, which the card uses for formatting, is
 * untouched. The alternative (shifting the synthetic day so real "now"
 * lands where we want) would put every axis label an hour or more out.
 * ---------------------------------------------------------------- */
const realDateNow = Date.now.bind(Date);
Date.now = () => {
  const d = new Date(realDateNow());
  d.setHours(0, 0, 0, 0);
  return d.getTime() + state.now * 60 * 1000;
};

function buildHass() {
  const midnight = midnightTs();
  const b = boundariesFor(midnight);
  const values = curveValues();
  const nowTs = midnight + state.now * 60;

  const phase = phaseAt(nowTs, b.morningTs, b.dayStartTs, b.eveningTs, b.nightTs);
  const targets = targetsForPhase(phase, nowTs, b, values);

  return {
    states: {
      'sun.sun': {
        state: 'above_horizon',
        attributes: {
          next_rising: new Date((midnight + SUNRISE_HOUR * 3600) * 1000).toISOString(),
          next_setting: new Date((midnight + state.sunset * 60) * 1000).toISOString(),
        },
      },
      // Matches sensor.py's _AdaptiveLightingSensor exactly: state is the
      // phase name, everything else is an attribute.
      'sensor.default_flare': {
        state: phase,
        attributes: {
          phase,
          brightness: targets.brightness,
          color_temp: targets.kelvin,
          morning_start: b.morningTs,
          day_start: b.dayStartTs,
          evening_start: b.eveningTs,
          night_start: b.nightTs,
          evening_earliest: midnight + state.evening_earliest * 60,
          evening_latest: midnight + state.evening_latest * 60,
          points: buildPoints(midnight, b, values),
        },
      },
    },
  };
}

let card = null;

function refresh() {
  if (card) card.hass = buildHass();
  updateReadouts();
  writeHash();
}

/** Explains where Evening actually landed, and which of the three
 *  inputs decided it - the bit of the schedule people most often expect
 *  to be a plain time and are surprised isn't. */
function updateReadouts() {
  const el = document.getElementById('alp-derived');
  if (!el) return;

  const { sunset, evening_earliest: earliest, evening_latest: latest } = state;
  const chosen = Math.max(earliest, Math.min(sunset, latest));

  let why;
  if (chosen === sunset) {
    why = `sunset (${fmtMinutes(sunset)}) falls between the earliest and latest bounds, so Evening simply tracks it`;
  } else if (chosen === earliest) {
    why = `sunset (${fmtMinutes(sunset)}) is <em>earlier</em> than the earliest bound, so Evening is held back to ${fmtMinutes(earliest)} — a dark winter afternoon doesn't drop the house into evening lighting`;
  } else {
    why = `sunset (${fmtMinutes(sunset)}) is <em>later</em> than the latest bound, so Evening is pulled forward to ${fmtMinutes(latest)} — a midsummer sunset doesn't mean evening never arrives`;
  }

  el.innerHTML = `<strong>Evening starts at ${fmtMinutes(chosen)}.</strong> It's the only boundary you don't set directly: ${why}.`;
}

/* ------------------------- URL hash state ------------------------ */

function writeHash() {
  const diff = Object.entries(state)
    .filter(([k, v]) => v !== DEFAULT_STATE[k])
    .map(([k, v]) => `${k}=${v}`);
  const hash = diff.length ? `#${diff.join('&')}` : '';
  // replaceState rather than assigning location.hash: dragging a slider
  // would otherwise push a history entry per frame.
  history.replaceState(null, '', `${location.pathname}${location.search}${hash}`);
}

function readHash() {
  const raw = location.hash.replace(/^#/, '');
  if (!raw) return;
  for (const pair of raw.split('&')) {
    const [k, v] = pair.split('=');
    if (k in DEFAULT_STATE && v !== undefined && Number.isFinite(Number(v))) {
      state[k] = Number(v);
    }
  }
}

/* --------------------------- rendering --------------------------- */

function makeControl({ key, label, entity }, { min, max, step, format }) {
  const wrap = document.createElement('div');
  wrap.className = 'alp-control';

  const id = `alp-${key}`;
  const labelEl = document.createElement('label');
  labelEl.setAttribute('for', id);
  labelEl.textContent = label;

  const out = document.createElement('output');
  out.setAttribute('for', id);
  out.textContent = format(state[key]);

  const input = document.createElement('input');
  input.type = 'range';
  input.id = id;
  input.min = min;
  input.max = max;
  input.step = step;
  input.value = state[key];
  input.addEventListener('input', () => {
    state[key] = Number(input.value);
    out.textContent = format(state[key]);
    refresh();
  });

  const entityEl = document.createElement('span');
  entityEl.className = 'alp-entity';
  entityEl.textContent = entity;

  wrap.append(labelEl, out, input, entityEl);
  wrap._sync = () => {
    input.value = state[key];
    out.textContent = format(state[key]);
  };
  return wrap;
}

const TIME_OPTS = { min: 0, max: 1439, step: 5, format: fmtMinutes };
const BRIGHTNESS_OPTS = {
  min: 0,
  max: 255,
  step: 1,
  // Brightness is 0-255 in HA's API but shown as a percentage in its UI;
  // showing both saves a mental conversion when copying a value across.
  format: (v) => `${v} (${Math.round((v / 255) * 100)}%)`,
};
const KELVIN_OPTS = { min: 1500, max: 8000, step: 50, format: (v) => `${v} K` };
const TRANSITION_OPTS = {
  min: 0,
  max: 1440,
  step: 5,
  format: fmtDuration,
};

const controlEls = [];

function mountControls() {
  const groups = [
    ['alp-times', TIME_CONTROLS, TIME_OPTS],
    ['alp-brightness', BRIGHTNESS_CONTROLS, BRIGHTNESS_OPTS],
    ['alp-kelvin', KELVIN_CONTROLS, KELVIN_OPTS],
    ['alp-transitions', TRANSITION_CONTROLS, TRANSITION_OPTS],
  ];
  for (const [containerId, controls, opts] of groups) {
    const container = document.getElementById(containerId);
    if (!container) continue;
    for (const control of controls) {
      const el = makeControl(control, opts);
      controlEls.push(el);
      container.appendChild(el);
    }
  }

  const nowInput = document.getElementById('alp-now');
  const nowOut = document.getElementById('alp-now-out');
  if (nowInput && nowOut) {
    nowInput.value = state.now;
    nowOut.textContent = fmtMinutes(state.now);
    nowInput.addEventListener('input', () => {
      state.now = Number(nowInput.value);
      nowOut.textContent = fmtMinutes(state.now);
      refresh();
    });
    controlEls.push({ _sync: () => {
      nowInput.value = state.now;
      nowOut.textContent = fmtMinutes(state.now);
    } });
  }

  document.querySelectorAll('[data-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      Object.assign(state, DEFAULT_STATE, PRESETS[btn.dataset.preset] || {});
      controlEls.forEach((el) => el._sync());
      refresh();
    });
  });

  const share = document.getElementById('alp-share');
  if (share) {
    share.addEventListener('click', async () => {
      const status = document.getElementById('alp-status');
      try {
        await navigator.clipboard.writeText(location.href);
        if (status) status.textContent = 'Link copied — it reopens this page with these exact settings.';
      } catch (err) {
        if (status) status.textContent = `Copy failed; the URL in your address bar already has these settings.`;
      }
    });
  }
}

async function main() {
  readHash();
  mountControls();

  const host = document.getElementById('alp-card-host');
  if (!host) return;

  try {
    await import(CARD_URL);
  } catch (err) {
    host.innerHTML = `<div class="alp-error"><strong>The chart couldn't load.</strong>
      This page renders the integration's real dashboard card, which is copied
      in from <code>custom_components/flare/www/</code> when
      the site is built. If you're building the site locally, run that copy step
      first — see CONTRIBUTING.md.</div>`;
    return;
  }

  // A minimal <ha-card> stand-in. The real one is part of Home
  // Assistant's frontend, which this repo neither ships nor should
  // depend on - same approach as dashboard/preview.html.
  if (!customElements.get('ha-card')) {
    customElements.define(
      'ha-card',
      class extends HTMLElement {
        connectedCallback() {
          if (this._built) return;
          this._built = true;
          this.style.cssText = `
            display: block;
            background: var(--card-background-color);
            border-radius: 12px;
            box-shadow: var(--ha-card-box-shadow);
            overflow: hidden;
          `;
          const header = this.getAttribute('header');
          if (header) {
            const h = document.createElement('div');
            h.textContent = header;
            h.style.cssText =
              'padding:16px 16px 0;font-size:1.15em;font-weight:500;color:var(--primary-text-color);';
            this.prepend(h);
          }
        }
      }
    );
  }

  card = document.createElement('flare-curve-card');
  card.setConfig({ title: 'Adaptive Lighting' });
  host.appendChild(card);
  refresh();
}

main();
