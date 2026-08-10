/**
 * Adaptive Lighting Curve — custom Lovelace card
 *
 * Renders the day's brightness + color-temperature curve as an actual
 * rendered-color chart (Kelvin -> RGB). The curve itself is NOT
 * recomputed here: it's read straight from sensor.adaptive_lighting_curve,
 * whose `points` attribute is produced by the adaptive_lighting_helpers
 * integration's curve.py (custom_components/adaptive_lighting_helpers/) -
 * the same module the compute_curve service uses. That's the single
 * source of truth; this card just displays it. The "now" marker reads
 * sensor.adaptive_lighting's phase/brightness/color_temp attributes
 * directly for the same reason - and unlike the curve, those DO follow
 * a manual override via select.adaptive_lighting_phase (see
 * coordinator.py), since they represent "right now" rather than the
 * full-day schedule.
 *
 * Each point also carries kelvin_rgb - what apply_lighting/
 * compute_lighting_groups would actually send as rgb_color to an
 * RGB-capable light with Prefer RGB Color on. Always identical to
 * kelvin for this integration's own curve sensor, so the extra "RGB
 * target" caps this card can draw only ever appear for a hand-written
 * points source that sets the two differently.
 */

const DEFAULT_ENTITIES = {
  morning: 'sensor.morning_start',
  day: 'sensor.day_start',
  evening: 'sensor.evening_start',
  night: 'sensor.night_start',
  // phase/brightness_now/kelvin_now all read from the same combined
  // entity by default (state = phase, attributes = brightness/color_temp) -
  // see the fallback in `set hass()` below for custom configs still
  // pointing at separate sensors.
  phase: 'sensor.adaptive_lighting',
  brightness_now: 'sensor.adaptive_lighting',
  kelvin_now: 'sensor.adaptive_lighting',
  sun: 'sun.sun',
  curve: 'sensor.adaptive_lighting_curve',
};

const VB_W = 960;
const VB_H = 220;
const PAD_L = 34;
const PAD_R = 12;
const PAD_TOP = 44;
const PAD_BOTTOM = 26;
const CHART_W = VB_W - PAD_L - PAD_R;
const CHART_H = VB_H - PAD_TOP - PAD_BOTTOM;
const BASELINE_Y = VB_H - PAD_BOTTOM;

function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

// Tanner Helland's Kelvin -> RGB approximation. Purely a display concern
// (turning a Kelvin number into a colour) -- not part of the schedule
// logic, so it stays here rather than in the shared macro.
function kelvinToRgb(kelvin) {
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

// brightness_now/kelvin_now default to the same combined entity as
// phase (state = phase, attributes = brightness/color_temp) - read the
// named attribute if present, falling back to .state for a custom
// config still pointing at a separate plain-value sensor.
function numFromAttrOrState(stateObj, attrName) {
  if (!stateObj) return undefined;
  const attrVal = stateObj.attributes && stateObj.attributes[attrName];
  return attrVal !== undefined ? Number(attrVal) : Number(stateObj.state);
}

// rgb_color is optional (only present for entities exposing it - see
// README's "Bring your own sensor") - null rather than a default when
// absent or malformed, so callers can tell "no RGB target" apart from
// "RGB target happens to equal the colour-temp one".
function rgbFromAttr(stateObj) {
  if (!stateObj) return null;
  const v = stateObj.attributes && stateObj.attributes.rgb_color;
  return Array.isArray(v) && v.length === 3 ? v.map(Number) : null;
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
    return { title: 'Adaptive Lighting Curve' };
  }

  setConfig(config) {
    this._config = config || {};
    this._entities = { ...DEFAULT_ENTITIES, ...(this._config.entities || {}) };
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

    const morning = get(e.morning);
    const day = get(e.day);
    const evening = get(e.evening);
    const night = get(e.night);
    if (!morning || !day || !evening || !night) {
      this._renderError(
        `Missing entity: ${[e.morning, e.day, e.evening, e.night]
          .filter((id) => !hass.states[id])
          .join(', ')}`
      );
      return;
    }

    const phase = get(e.phase);
    const sun = get(e.sun);
    const curve = get(e.curve);
    const brightnessNow = get(e.brightness_now);
    const kelvinNow = get(e.kelvin_now);

    const boundaries = {
      morning: Number(morning.attributes.timestamp),
      day: Number(day.attributes.timestamp),
      evening: Number(evening.attributes.timestamp),
      night: Number(night.attributes.timestamp),
      // Only present on sensor.evening_start - see coordinator.py's
      // evening_earliest_ts/evening_latest_ts. Absent (null) if the
      // integration's evening_earliest_time/evening_latest_time
      // weren't configured.
      eveningEarliest: evening.attributes.earliest != null ? Number(evening.attributes.earliest) : null,
      eveningLatest: evening.attributes.latest != null ? Number(evening.attributes.latest) : null,
    };

    const pointsRaw = curve && curve.attributes && curve.attributes.points;
    // brightness_now/kelvin_now default to the same combined entity as
    // phase, read via its brightness/color_temp attributes; a custom
    // config pointing them at separate plain-value sensors still works
    // by falling back to .state.
    const brightnessNowValue = numFromAttrOrState(brightnessNow, 'brightness');
    const kelvinNowValue = numFromAttrOrState(kelvinNow, 'color_temp');
    // kelvinNow is the same entity as phase by default (sensor.adaptive_lighting) -
    // its rgb_color attribute is present whenever the pointed-at sensor
    // exposes one (see README's "Bring your own sensor").
    const rgbColorNowValue = rgbFromAttr(kelvinNow);

    const cacheKey = JSON.stringify([
      boundaries,
      phase && phase.state,
      sun && sun.attributes.next_setting,
      sun && sun.attributes.next_rising,
      pointsRaw,
      brightnessNowValue,
      kelvinNowValue,
      rgbColorNowValue,
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
    this._rgbColorNow = rgbColorNowValue;

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
      <ha-card header="${this._config.title || 'Adaptive Lighting Curve'}">
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
    let rgbCaps = '';
    let haveRgbDivergence = false;
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

      // A thin cap above each bar wherever the RGB target actually
      // diverges from the colour-temp one - nothing renders here for
      // this integration's own curve sensor, since kelvin_rgb always
      // equals kelvin there now; kept in case anything ever points this
      // card at a hand-written points source that sets the two
      // differently.
      const RGB_CAP_H = 8;
      const diverging = samples.filter((s) => s.kelvin_rgb != null && s.kelvin_rgb !== s.kelvin);
      haveRgbDivergence = diverging.length > 0;
      rgbCaps = diverging
        .map((s) => {
          const x = xOf(s.t);
          const h = hOf(s.brightness);
          const color = rgbToHex(kelvinToRgb(s.kelvin_rgb));
          const y = BASELINE_Y - h - RGB_CAP_H;
          return `<rect x="${(x - barW / 2).toFixed(2)}" y="${y.toFixed(2)}" width="${barW.toFixed(2)}" height="${RGB_CAP_H}" fill="${color}" class="rgb-cap" />`;
        })
        .join('');
    }

    const hourTicks = [];
    for (let h = 0; h <= 24; h += 3) {
      const t = dayStart + h * 3600;
      hourTicks.push(
        `<text x="${xOf(t).toFixed(1)}" y="${VB_H - 6}" class="axis-label" text-anchor="middle">${String(h).padStart(2, '0')}:00</text>`
      );
    }

    // Shaded band + edge lines showing where Evening is allowed to land
    // (clamped between earliest/latest, tracking sunset in between) -
    // makes it visible at a glance why Evening's own boundary line is
    // where it is, rather than only explained in the footnote text.
    let clampBand = '';
    if (b.eveningEarliest != null && b.eveningLatest != null) {
      const xEarliest = xOf(b.eveningEarliest);
      const xLatest = xOf(b.eveningLatest);
      clampBand = `
        <rect x="${Math.min(xEarliest, xLatest).toFixed(2)}" y="${PAD_TOP}" width="${Math.abs(xLatest - xEarliest).toFixed(2)}" height="${(BASELINE_Y - PAD_TOP).toFixed(2)}" class="clamp-band" />
        <line x1="${xEarliest.toFixed(1)}" y1="${PAD_TOP}" x2="${xEarliest.toFixed(1)}" y2="${BASELINE_Y}" class="clamp-edge" />
        <line x1="${xLatest.toFixed(1)}" y1="${PAD_TOP}" x2="${xLatest.toFixed(1)}" y2="${BASELINE_Y}" class="clamp-edge" />
      `;
    }

    const boundaryDefs = [
      ['Morning', b.morning],
      ['Day', b.day],
      ['Evening', b.evening],
      ['Night', b.night],
    ];
    const boundaryLines = boundaryDefs
      .map(([label, t], i) => {
        const x = xOf(t).toFixed(1);
        const anchorPos = i === 0 ? 'start' : i === boundaryDefs.length - 1 ? 'end' : 'middle';
        // Alternate rows so adjacent boundaries (e.g. Morning/Day only 2h
        // apart, or a short Evening span) don't render their labels on top
        // of each other.
        const labelY = i % 2 === 0 ? PAD_TOP - 26 : PAD_TOP - 10;
        return `
          <line x1="${x}" y1="${PAD_TOP}" x2="${x}" y2="${BASELINE_Y}" class="boundary-line" />
          <text x="${x}" y="${labelY}" class="boundary-label" text-anchor="${anchorPos}">${label} ${fmtTime(t)}</text>
        `;
      })
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
      // Small ring above the main dot for the RGB target, only when the
      // sensor actually provides one and it's visibly different from
      // the colour-temp target - see rgbFromAttr().
      if (this._rgbColorNow) {
        const nowRgbHex = rgbToHex(this._rgbColorNow);
        if (nowRgbHex !== nowHex) {
          haveRgbDivergence = true;
          nowMarker += `<circle cx="${nowX}" cy="${(nowY - 10).toFixed(1)}" r="3" fill="${nowRgbHex}" class="now-rgb-dot" />`;
        }
      }
    } else if (nowInWindow) {
      nowMarker = `<line x1="${nowX}" y1="${PAD_TOP - 4}" x2="${nowX}" y2="${BASELINE_Y}" class="now-line" />`;
    }

    const svg = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="none" class="chart">
        ${bars}
        ${clampBand}
        ${rgbCaps}
        ${boundaryLines}
        ${hourTicks.join('')}
        ${sunMarkers}
        ${nowMarker}
        <line x1="${PAD_L}" y1="${BASELINE_Y}" x2="${VB_W - PAD_R}" y2="${BASELINE_Y}" class="axis-line" />
      </svg>
    `;

    const rgbLegend = haveRgbDivergence
      ? `<div class="rgb-legend"><span class="rgb-swatch"></span>RGB target (Prefer RGB Color) where warmer than colour temperature can reach</div>`
      : '';

    const nowLabel = haveNow
      ? `Now ${fmtTime(now)} · ${this._brightnessNow} bri · ${this._kelvinNow}K${this._phaseState ? ` · ${this._phaseState}` : ''}`
      : this._phaseState || '';

    const footnote = haveSamples
      ? this._eveningNote()
      : `${this._eveningNote()} (curve still populating — it updates every 10 minutes or on a boundary change)`;

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .card-content { padding: 8px 16px 12px; position: relative; }
        .now-label {
          font-size: 0.85em;
          color: var(--secondary-text-color);
          margin-bottom: 2px;
        }
        .chart { width: 100%; height: 220px; display: block; }
        .axis-line { stroke: var(--divider-color, #888); stroke-width: 1; }
        .axis-label { fill: var(--secondary-text-color); font-size: 11px; }
        .clamp-band { fill: var(--secondary-text-color); opacity: 0.18; }
        .clamp-edge {
          stroke: var(--secondary-text-color);
          stroke-width: 1;
          stroke-dasharray: 1 3;
          opacity: 0.6;
        }
        .rgb-cap { opacity: 0.95; }
        .now-rgb-dot { stroke: var(--card-background-color); stroke-width: 1; }
        .rgb-legend {
          font-size: 0.78em;
          color: var(--secondary-text-color);
          margin-top: 2px;
        }
        .rgb-swatch {
          display: inline-block;
          width: 8px;
          height: 8px;
          border-radius: 2px;
          background: linear-gradient(135deg, #ff9f45, #ffd7a8);
          margin-right: 5px;
          vertical-align: middle;
        }
        .boundary-line {
          stroke: var(--secondary-text-color);
          stroke-width: 1;
          stroke-dasharray: 3 3;
          opacity: 0.6;
        }
        .boundary-label { fill: var(--primary-text-color); font-size: 11px; }
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
      <ha-card header="${this._config.title || 'Adaptive Lighting Curve'}">
        <div class="card-content">
          <div class="now-label">${nowLabel}</div>
          ${sunLabel ? `<div class="sun-label">${sunLabel}</div>` : ''}
          ${svg}
          <div class="tooltip"></div>
          <div class="footnote">${footnote}</div>
          ${rgbLegend}
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
      let rgbPart = '';
      if (s.kelvin_rgb != null && s.kelvin_rgb !== s.kelvin) {
        const rgbHex = rgbToHex(kelvinToRgb(s.kelvin_rgb));
        const rgbSwatch = `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${rgbHex};vertical-align:middle;"></span>`;
        rgbPart = ` &nbsp; RGB ${s.kelvin_rgb}K ${rgbSwatch}`;
      }
      this._tooltipEl.style.display = 'block';
      this._tooltipEl.style.left = `${clamp((clientX - rect.left), 0, rect.width - 160)}px`;
      this._tooltipEl.style.top = '4px';
      this._tooltipEl.innerHTML = `<b>${fmtTime(s.t)}</b> &nbsp; ${s.brightness} bri &nbsp; ${s.kelvin}K ${swatch}${rgbPart}`;
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
