/**
 * Adaptive Lighting Curve — custom Lovelace card
 *
 * Renders the day's brightness + color-temperature curve as an actual
 * rendered-color chart (Kelvin -> RGB). The curve itself is NOT
 * recomputed here: it's read straight off the combined sensor's
 * `points` attribute, produced by the adaptive_lighting_helpers
 * integration's curve.py (custom_components/adaptive_lighting_helpers/) -
 * the same module the compute_curve service uses. That's the single
 * source of truth; this card just displays it. The "now" marker reads
 * the same sensor's phase/brightness/color_temp attributes directly -
 * and unlike the curve, those DO follow a manual override via the phase
 * select (see coordinator.py), since they represent "right now" rather
 * than the full-day schedule.
 */

const DEFAULT_ENTITIES = {
  // The entity_ids the auto-seeded "Default" sensor creates (every
  // sensor's entities are prefixed with its slugified name - see
  // coordinator.py's schedule_instances()). Point a card at any other
  // sensor by overriding these via the card config's `entities` map.
  // phase/brightness_now/kelvin_now all read from the same combined
  // entity by default (state = phase, attributes = brightness/color_temp/
  // morning_start/day_start/evening_start/night_start/evening_earliest/
  // evening_latest/points - see sensor.py's _AdaptiveLightingSensor) -
  // see the fallback in `set hass()` below for custom configs still
  // pointing at separate sensors.
  phase: 'sensor.default_adaptive_lighting',
  brightness_now: 'sensor.default_adaptive_lighting',
  kelvin_now: 'sensor.default_adaptive_lighting',
  sun: 'sun.sun',
};

const VB_W = 960;
const VB_H = 220;
const PAD_L = 34;
const PAD_R = 12;
const PAD_TOP = 24;
const PAD_BOTTOM = 26;
const CHART_W = VB_W - PAD_L - PAD_R;
const CHART_H = VB_H - PAD_TOP - PAD_BOTTOM;
const BASELINE_Y = VB_H - PAD_BOTTOM;
// Evening's earliest/latest range ticks (see eveningRange below) hang
// down from the x-axis, the same direction a normal axis tick would.
const RANGE_TICK = 6;

function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

// Tanner Helland's Kelvin -> RGB approximation. Purely a display concern
// (turning a Kelvin number into a colour) -- not part of the schedule
// logic, so it stays here rather than in the shared macro.
export function kelvinToRgb(kelvin) {
  const temp = kelvin / 100;
  let r, g, b;

  if (temp <= 66) {
    r = 255;
  } else {
    r = clamp(329.698727446 * Math.pow(temp - 60, -0.1332047592), 0, 255);
  }

  if (temp <= 66) {
    g = clamp(99.4708025861 * Math.log(temp) - 161.1195681661, 0, 255);
  } else {
    g = clamp(288.1221695283 * Math.pow(temp - 60, -0.0755148492), 0, 255);
  }

  if (temp >= 66) {
    b = 255;
  } else if (temp <= 19) {
    b = 0;
  } else {
    b = clamp(138.5177312231 * Math.log(temp - 10) - 305.0447927307, 0, 255);
  }

  return [Math.round(r), Math.round(g), Math.round(b)];
}

function rgbToHex([r, g, b]) {
  return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
}

// Mirrors curve.py's phase_at() exactly - same four boundaries, same
// half-open intervals - so the hover tooltip's phase name always agrees
// with what the real schedule would report for that instant.
//
// Exported (along with phaseMarks and kelvinToRgb below) purely so
// tests/test_curve_js_parity.py can import this file under node and
// check them against curve.py. Home Assistant loads this file as an ES
// module (add_extra_js_url defaults to es5: false), so the exports are
// simply unused there.
export function phaseAt(t, morning, day, evening, night) {
  if (t < morning) return 'Night';
  if (t < day) return 'Morning';
  if (t < evening) return 'Day';
  if (t < night) return 'Evening';
  return 'Night';
}

const PHASE_ORDER = ['Morning', 'Day', 'Evening', 'Night'];

// Mirrors curve.py's phase_marks(). Which phases actually occur today,
// and where each really starts - which is NOT the same as the four
// configured boundaries, because phaseAt above is a cascade of `t <
// boundary` tests that tolerates them being set out of order.
//
// With Morning at 10:00 and Day at 08:00, nothing is ever Morning (by
// the time t clears 10:00 it has already passed the 08:00 test), and Day
// actually begins at 10:00 rather than its own 08:00. Drawing the raw
// boundaries would label a phase that isn't happening, and put the next
// one at a time it doesn't start. Both fall out of one rule: each phase
// begins at the running maximum of the boundaries up to and including
// its own, and is real exactly when the span to the next one isn't
// empty.
//
// Night is special both ways: the day always opens on Night, so its
// boundary marks the RETURN to Night - and if nothing ever leaves Night
// (a fully reversed schedule) there's no return to draw, so nothing is
// marked at all. See curve.py's phase_marks docstring, and
// tests/test_curve.py, which pins this against phaseAt's real output.
export function phaseMarks(morning, day, evening, night) {
  const effectiveStarts = [];
  let running = null;
  for (const ts of [morning, day, evening, night]) {
    running = running === null ? ts : Math.max(running, ts);
    effectiveStarts.push(running);
  }

  const marks = [];
  for (let i = 0; i < PHASE_ORDER.length - 1; i++) {
    if (effectiveStarts[i] < effectiveStarts[i + 1]) {
      marks.push([PHASE_ORDER[i], effectiveStarts[i]]);
    }
  }
  if (marks.length) {
    marks.push(['Night', effectiveStarts[effectiveStarts.length - 1]]);
  }
  return marks;
}

// title unset -> "Adaptive Lighting"; title: "" explicitly -> no header at
// all (ha-card renders nothing for a falsy header) - lets a dashboard that
// already labels each sensor elsewhere (a heading card, a section title)
// suppress the card's own redundant one, while everyone else still gets a
// sensible default with zero config.
function cardHeader(config) {
  return config.title === '' ? '' : config.title || 'Adaptive Lighting';
}

// brightness_now/kelvin_now default to the same combined entity as
// phase (state = phase, attributes = brightness/color_temp) - read the
// named attribute if present, falling back to .state for a custom
// config still pointing at a separate plain-value sensor.
function numFromAttrOrState(stateObj, attrName) {
  if (!stateObj) return undefined;
  const attrVal = stateObj.attributes && stateObj.attributes[attrName];
  return attrVal !== undefined ? Number(attrVal) : Number(stateObj.state);
}

function fmtTime(tSec) {
  const d = new Date(tSec * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// sun.sun's next_rising/next_setting are exactly that - "next" - so
// whichever of the two already happened today has rolled over to
// tomorrow's occurrence by the time we read it. The chart only ever
// shows one calendar day, so shift by the one day that lands the
// timestamp back inside today's window rather than trusting it as-is.
function sunTimeInWindow(isoString, dayStart, dayEnd) {
  if (!isoString) return null;
  let t = new Date(isoString).getTime() / 1000;
  if (t < dayStart) t += 86400;
  else if (t >= dayEnd) t -= 86400;
  return t;
}

class AdaptiveLightingCurveCard extends HTMLElement {
  static getStubConfig() {
    return { title: 'Adaptive Lighting' };
  }

  setConfig(config) {
    this._config = config || {};
    // `sensor: living_room` is shorthand for pointing every entity at
    // that named sensor's entities (the value is the sensor's slugified
    // name, i.e. its entity_id prefix - see coordinator.py's
    // schedule_instances()). An explicit `entities` map still overrides
    // individual ids on top.
    const fromSensor = {};
    if (this._config.sensor) {
      const base = `sensor.${this._config.sensor}_adaptive_lighting`;
      fromSensor.phase = base;
      fromSensor.brightness_now = base;
      fromSensor.kelvin_now = base;
    }
    this._entities = { ...DEFAULT_ENTITIES, ...fromSensor, ...(this._config.entities || {}) };
    this._cacheKey = null;
    this._samples = [];
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
    }
  }

  getCardSize() {
    return 4;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._render(), 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  set hass(hass) {
    this._hass = hass;
    const e = this._entities;
    const get = (id) => hass.states[id];

    const phase = get(e.phase);
    if (!phase) {
      this._renderError(`Missing entity: ${e.phase}`);
      return;
    }

    const sun = get(e.sun);
    const brightnessNow = get(e.brightness_now);
    const kelvinNow = get(e.kelvin_now);

    // Today's four phase-boundary timestamps live as attributes on the
    // phase entity itself (see sensor.py's _AdaptiveLightingSensor) -
    // there's no separate sensor.morning_start/day_start/evening_start/
    // night_start any more.
    const boundaries = {
      morning: Number(phase.attributes.morning_start),
      day: Number(phase.attributes.day_start),
      evening: Number(phase.attributes.evening_start),
      night: Number(phase.attributes.night_start),
      // Absent (null) if the sensor's evening_earliest_time/
      // evening_latest_time weren't configured.
      eveningEarliest: phase.attributes.evening_earliest != null ? Number(phase.attributes.evening_earliest) : null,
      eveningLatest: phase.attributes.evening_latest != null ? Number(phase.attributes.evening_latest) : null,
    };

    const pointsRaw = phase.attributes.points;
    // brightness_now/kelvin_now default to the same combined entity as
    // phase, read via its brightness/color_temp attributes; a custom
    // config pointing them at separate plain-value sensors still works
    // by falling back to .state.
    const brightnessNowValue = numFromAttrOrState(brightnessNow, 'brightness');
    const kelvinNowValue = numFromAttrOrState(kelvinNow, 'color_temp');

    const cacheKey = JSON.stringify([
      boundaries,
      phase && phase.state,
      sun && sun.attributes.next_setting,
      sun && sun.attributes.next_rising,
      pointsRaw,
      brightnessNowValue,
      kelvinNowValue,
    ]);

    if (cacheKey === this._cacheKey) {
      return;
    }
    this._cacheKey = cacheKey;

    this._boundaries = boundaries;
    this._phaseState = phase && phase.state;
    this._sun = sun;
    this._brightnessNow = brightnessNowValue;
    this._kelvinNow = kelvinNowValue;

    if (pointsRaw) {
      // HA's trigger-template attribute rendering sometimes preserves the
      // native list from `| tojson` instead of leaving it as a JSON string
      // (observed: state_attr(...) is string => false) -- accept either.
      let parsed = null;
      if (Array.isArray(pointsRaw)) {
        parsed = pointsRaw;
      } else if (typeof pointsRaw === 'string') {
        try {
          parsed = JSON.parse(pointsRaw);
        } catch (err) {
          // Keep the last good samples; don't blank the chart over a
          // transient parse hiccup.
          parsed = null;
        }
      }
      if (Array.isArray(parsed) && parsed.length > 1) {
        this._samples = parsed;
      }
    }

    this._render();
  }

  _renderError(message) {
    this.shadowRoot.innerHTML = `
      <ha-card header="${cardHeader(this._config)}">
        <div style="padding: 16px; color: var(--error-color, red);">${message}</div>
      </ha-card>
    `;
  }

  _eveningNote() {
    const b = this._boundaries;
    if (!b) return '';
    const eveningTime = fmtTime(b.evening);
    if (b.eveningEarliest == null || b.eveningLatest == null || !this._sun || !this._sun.attributes.next_setting) {
      return `Evening starts at ${eveningTime}.`;
    }
    const earliestTs = b.eveningEarliest;
    const latestTs = b.eveningLatest;
    const sunsetTs = new Date(this._sun.attributes.next_setting).getTime() / 1000;

    let reason;
    if (Math.abs(b.evening - latestTs) < 60 && sunsetTs > latestTs) {
      reason = `capped at its latest bound (${fmtTime(latestTs)}) — tonight's sunset is later, at ${fmtTime(sunsetTs)}`;
    } else if (Math.abs(b.evening - earliestTs) < 60 && sunsetTs < earliestTs) {
      reason = `capped at its earliest bound (${fmtTime(earliestTs)}) — tonight's sunset is earlier, at ${fmtTime(sunsetTs)}`;
    } else {
      reason = `following tonight's sunset`;
    }
    return `Evening starts at ${eveningTime}, ${reason}.`;
  }

  _render() {
    if (!this._boundaries) return;
    const b = this._boundaries;
    const samples = this._samples;
    const haveSamples = samples && samples.length > 1;

    // Prefer the sample window's own bounds; fall back to a midnight-anchored
    // guess (for axis/boundary-line layout only) if the curve sensor hasn't
    // populated yet.
    let dayStart;
    let dayEnd;
    if (haveSamples) {
      dayStart = samples[0].t;
      dayEnd = samples[samples.length - 1].t;
    } else {
      const anchor = new Date(b.morning * 1000);
      anchor.setHours(0, 0, 0, 0);
      dayStart = anchor.getTime() / 1000;
      dayEnd = dayStart + 86400;
    }
    const span = dayEnd - dayStart;

    const xOf = (t) => PAD_L + ((t - dayStart) / span) * CHART_W;
    const hOf = (brightness) => (brightness / 255) * CHART_H;

    let bars = '';
    if (haveSamples) {
      const barW = CHART_W / (samples.length - 1) + 0.6;
      bars = samples
        .map((s) => {
          const x = xOf(s.t);
          const h = hOf(s.brightness);
          const color = rgbToHex(kelvinToRgb(s.kelvin));
          return `<rect x="${(x - barW / 2).toFixed(2)}" y="${(BASELINE_Y - h).toFixed(2)}" width="${barW.toFixed(2)}" height="${h.toFixed(2)}" fill="${color}" />`;
        })
        .join('');
    }

    // Rendered as real HTML text, not SVG <text>, deliberately - the
    // chart's viewBox is stretched to the card's actual width via
    // preserveAspectRatio="none" (a non-uniform scale, since only x
    // stretches - the SVG's height is pinned to a fixed 220px), and SVG
    // text glyphs get stretched/squished by that same transform along
    // with everything else, unlike real HTML text which always renders
    // at native scale. left:% positions each label proportionally
    // within the chart-wrap div, which is exactly as wide as the SVG
    // regardless of actual pixels, so this still lines up with xOf(t).
    const hourLabels = [];
    for (let h = 0; h <= 24; h += 3) {
      const t = dayStart + h * 3600;
      const leftPct = ((xOf(t) / VB_W) * 100).toFixed(2);
      hourLabels.push(`<span class="axis-label" style="left:${leftPct}%">${String(h).padStart(2, '0')}:00</span>`);
    }

    // Phase name labels - real HTML text for the same squishing reason
    // the hour ticks are (see hourLabels above), and name-only rather
    // than "Morning 06:00" - the exact time is a native hover tooltip
    // (`title`) instead of always-on text, which is what made four of
    // these side by side illegible in a narrow card in the first place.
    // All four are treated identically, Evening included - it centres
    // on its own boundary line just like Morning/Day/Night; the
    // earliest/latest range it can be clamped between is shown
    // separately, by eveningRange below, not folded into this label.
    // Only the phases that actually occur, each at the instant it really
    // starts - see phaseMarks above. A schedule whose boundaries are out
    // of order otherwise labels a phase that never happens.
    const marks = phaseMarks(b.morning, b.day, b.evening, b.night);

    const topLabels = marks
      .map(([name, t]) => {
        const leftPct = ((xOf(t) / VB_W) * 100).toFixed(2);
        return `<span class="boundary-label" style="left:${leftPct}%" title="${fmtTime(t)}">${name}</span>`;
      })
      .join('');

    // Evening's boundary is a *range* (clamped between earliest/latest,
    // tracking sunset in between), not a single instant like the other
    // three phases - anchored on the chart as two short ticks hanging
    // off the x-axis at its earliest/latest bound, the same direction a
    // normal axis tick would point. No connecting bracket between them -
    // that read as a shape competing with the line+label convention the
    // other three phases use, rather than a plain axis annotation like
    // this. Falls back to nothing (same as Morning/Day/Night, which have
    // no range to show) if earliest/latest aren't configured.
    let eveningRange = '';
    if (b.eveningEarliest != null && b.eveningLatest != null) {
      const xEarliest = xOf(b.eveningEarliest);
      const xLatest = xOf(b.eveningLatest);
      eveningRange = `
        <g class="evening-range">
          <title>Evening window: ${fmtTime(b.eveningEarliest)} – ${fmtTime(b.eveningLatest)}</title>
          <line x1="${xEarliest.toFixed(1)}" y1="${BASELINE_Y}" x2="${xEarliest.toFixed(1)}" y2="${BASELINE_Y + RANGE_TICK}" />
          <line x1="${xLatest.toFixed(1)}" y1="${BASELINE_Y}" x2="${xLatest.toFixed(1)}" y2="${BASELINE_Y + RANGE_TICK}" />
        </g>
      `;
    }

    // Rendered after sunMarkers in the svg template below (not right
    // here where it's built) - Evening commonly starts at exactly
    // sunset (whenever sunset itself falls inside the earliest/latest
    // window, Evening just follows it directly), so the two lines are
    // often pixel-coincident. Drawing this dashed line on top of the
    // sun-line's solid stroke lets both remain visible - the dashes'
    // gaps show the orange line underneath instead of one flat-out
    // hiding the other.
    const boundaryLines = marks
      .map(([, t]) => `<line x1="${xOf(t).toFixed(1)}" y1="${PAD_TOP}" x2="${xOf(t).toFixed(1)}" y2="${BASELINE_Y}" class="boundary-line" />`)
      .join('');

    const sunriseTs = this._sun && sunTimeInWindow(this._sun.attributes.next_rising, dayStart, dayEnd);
    const sunsetTs = this._sun && sunTimeInWindow(this._sun.attributes.next_setting, dayStart, dayEnd);
    const sunMarkers = [
      sunriseTs != null ? ['Sunrise', sunriseTs] : null,
      sunsetTs != null ? ['Sunset', sunsetTs] : null,
    ]
      .filter(Boolean)
      .map(([, t]) => {
        const x = xOf(t).toFixed(1);
        return `
          <line x1="${x}" y1="${PAD_TOP}" x2="${x}" y2="${BASELINE_Y}" class="sun-line" />
          <circle cx="${x}" cy="${PAD_TOP}" r="3" class="sun-dot" />
        `;
      })
      .join('');
    const sunLabel = [
      sunriseTs != null ? `Sunrise ${fmtTime(sunriseTs)}` : null,
      sunsetTs != null ? `Sunset ${fmtTime(sunsetTs)}` : null,
    ]
      .filter(Boolean)
      .join(' · ');

    const now = Date.now() / 1000;
    const nowInWindow = now >= dayStart && now <= dayEnd;
    const haveNow = nowInWindow && Number.isFinite(this._brightnessNow) && Number.isFinite(this._kelvinNow);
    const nowX = xOf(now).toFixed(1);
    let nowMarker = '';
    if (haveNow) {
      const nowHex = rgbToHex(kelvinToRgb(this._kelvinNow));
      const nowY = BASELINE_Y - hOf(this._brightnessNow);
      nowMarker = `
        <line x1="${nowX}" y1="${PAD_TOP - 4}" x2="${nowX}" y2="${BASELINE_Y}" class="now-line" />
        <circle cx="${nowX}" cy="${nowY.toFixed(1)}" r="4" fill="${nowHex}" class="now-dot" />
      `;
    } else if (nowInWindow) {
      nowMarker = `<line x1="${nowX}" y1="${PAD_TOP - 4}" x2="${nowX}" y2="${BASELINE_Y}" class="now-line" />`;
    }

    const svg = `
      <div class="chart-wrap">
        <svg viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="none" class="chart">
          ${bars}
          ${sunMarkers}
          ${boundaryLines}
          ${nowMarker}
          <line x1="${PAD_L}" y1="${BASELINE_Y}" x2="${VB_W - PAD_R}" y2="${BASELINE_Y}" class="axis-line" />
          ${eveningRange}
        </svg>
        ${topLabels}
        ${hourLabels.join('')}
      </div>
    `;

    const nowLabel = haveNow
      ? `Now ${fmtTime(now)} · ${this._brightnessNow} bri · ${this._kelvinNow}K${this._phaseState ? ` · ${this._phaseState}` : ''}`
      : this._phaseState || '';

    const footnote = haveSamples
      ? this._eveningNote()
      : `${this._eveningNote()} (curve still populating — it refreshes every minute)`;

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .card-content { padding: 8px 16px 12px; position: relative; }
        .now-label {
          font-size: 0.85em;
          color: var(--secondary-text-color);
          margin-bottom: 2px;
        }
        .chart-wrap { position: relative; }
        .chart { width: 100%; height: 220px; display: block; }
        .axis-line { stroke: var(--divider-color, #888); stroke-width: 1; }
        .axis-label {
          position: absolute;
          bottom: 6px;
          transform: translateX(-50%);
          color: var(--secondary-text-color);
          font-size: 11px;
          white-space: nowrap;
        }
        .evening-range line {
          stroke: var(--secondary-text-color);
          stroke-width: 1.5;
          opacity: 0.7;
        }
        .boundary-line {
          stroke: var(--secondary-text-color);
          stroke-width: 1;
          stroke-dasharray: 3 3;
          opacity: 0.6;
        }
        .boundary-label {
          position: absolute;
          top: 2px;
          transform: translateX(-50%);
          color: var(--secondary-text-color);
          font-size: 11px;
          white-space: nowrap;
          cursor: default;
        }
        .now-line { stroke: var(--primary-text-color); stroke-width: 1.5; }
        .now-dot { stroke: var(--card-background-color); stroke-width: 1.5; }
        .sun-line { stroke: #f5a623; stroke-width: 1.5; opacity: 0.85; }
        .sun-dot { fill: #f5a623; }
        .sun-label { font-size: 0.78em; color: var(--secondary-text-color); margin-bottom: 2px; }
        .footnote {
          font-size: 0.78em;
          color: var(--secondary-text-color);
          margin-top: 6px;
        }
        .tooltip {
          position: absolute;
          pointer-events: none;
          background: var(--card-background-color);
          border: 1px solid var(--divider-color);
          border-radius: 6px;
          padding: 4px 8px;
          font-size: 0.78em;
          color: var(--primary-text-color);
          box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.3));
          display: none;
          white-space: nowrap;
          z-index: 2;
        }
      </style>
      <ha-card header="${cardHeader(this._config)}">
        <div class="card-content">
          <div class="now-label">${nowLabel}</div>
          ${sunLabel ? `<div class="sun-label">${sunLabel}</div>` : ''}
          ${svg}
          <div class="tooltip"></div>
          <div class="footnote">${footnote}</div>
        </div>
      </ha-card>
    `;

    this._svgEl = this.shadowRoot.querySelector('svg.chart');
    this._tooltipEl = this.shadowRoot.querySelector('.tooltip');
    this._dayStart = dayStart;
    this._span = span;

    if (!haveSamples) return;

    const onMove = (ev) => {
      const rect = this._svgEl.getBoundingClientRect();
      const clientX = ev.touches ? ev.touches[0].clientX : ev.clientX;
      const frac = clamp((clientX - rect.left) / rect.width, 0, 1);
      const t = this._dayStart + frac * this._span;
      let idx = 0;
      let bestDiff = Infinity;
      for (let i = 0; i < samples.length; i++) {
        const diff = Math.abs(samples[i].t - t);
        if (diff < bestDiff) {
          bestDiff = diff;
          idx = i;
        }
      }
      const s = samples[idx];
      const hex = rgbToHex(kelvinToRgb(s.kelvin));
      const swatch = `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${hex};vertical-align:middle;"></span>`;
      const phase = phaseAt(s.t, b.morning, b.day, b.evening, b.night);
      // sunriseTs/sunsetTs are closed over from _render()'s own scope -
      // already shifted into this same display window (see
      // sunTimeInWindow), so a plain interval check is enough.
      const sunState = sunriseTs != null && sunsetTs != null ? (s.t >= sunriseTs && s.t < sunsetTs ? 'Sun up' : 'Sun down') : '';
      this._tooltipEl.style.display = 'block';
      this._tooltipEl.style.left = `${clamp((clientX - rect.left), 0, rect.width - 170)}px`;
      this._tooltipEl.style.top = '4px';
      this._tooltipEl.innerHTML = `<b>${phase} · ${fmtTime(s.t)}${sunState ? ` · ${sunState}` : ''}</b><br>${s.brightness} bri &nbsp; ${s.kelvin}K ${swatch}`;
    };
    const onLeave = () => {
      this._tooltipEl.style.display = 'none';
    };

    this._svgEl.addEventListener('pointermove', onMove);
    this._svgEl.addEventListener('pointerleave', onLeave);
    this._svgEl.addEventListener('touchmove', onMove, { passive: true });
    this._svgEl.addEventListener('touchend', onLeave);
  }
}

customElements.define('adaptive-lighting-curve-card', AdaptiveLightingCurveCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'adaptive-lighting-curve-card',
  name: 'Adaptive Lighting Curve',
  description: 'Live brightness + rendered-color curve for the solar adaptive lighting schedule.',
});
