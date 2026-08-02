/**
 * Adaptive Lighting Curve — custom Lovelace card
 *
 * Renders the day's brightness + color-temperature curve as an actual
 * rendered-color chart (Kelvin -> RGB). The curve itself is NOT
 * recomputed here: it's read straight from sensor.adaptive_lighting_curve,
 * whose `points` attribute is produced by the same Jinja macros
 * (custom_templates/adaptive_lighting.jinja) that drive the real
 * production sensors in packages/adaptive_lighting.yaml. That's the
 * single source of truth; this card just displays it. The "now" marker
 * reads the live instantaneous sensors directly for the same reason.
 */

const DEFAULT_ENTITIES = {
  morning: 'sensor.morning_start',
  day: 'sensor.day_start',
  evening: 'sensor.evening_start',
  night: 'sensor.night_start',
  phase: 'sensor.day_phase',
  evening_earliest: 'input_datetime.evening_earliest',
  evening_latest: 'input_datetime.evening_latest',
  sun: 'sun.sun',
  curve: 'sensor.adaptive_lighting_curve',
  brightness_now: 'sensor.solar_adaptive_lighting_brightness',
  kelvin_now: 'sensor.solar_adaptive_lighting_color_temperature',
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

function todayAtLocal(anchorDate, hhmmss) {
  const [h, m, s] = hhmmss.split(':').map(Number);
  const d = new Date(anchorDate);
  d.setHours(h, m, s || 0, 0);
  return d.getTime() / 1000;
}

function fmtTime(tSec) {
  const d = new Date(tSec * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
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
    const eveningEarliest = get(e.evening_earliest);
    const eveningLatest = get(e.evening_latest);
    const sun = get(e.sun);
    const curve = get(e.curve);
    const brightnessNow = get(e.brightness_now);
    const kelvinNow = get(e.kelvin_now);

    const boundaries = {
      morning: Number(morning.attributes.timestamp),
      day: Number(day.attributes.timestamp),
      evening: Number(evening.attributes.timestamp),
      night: Number(night.attributes.timestamp),
    };

    const pointsRaw = curve && curve.attributes && curve.attributes.points;

    const cacheKey = JSON.stringify([
      boundaries,
      phase && phase.state,
      eveningEarliest && eveningEarliest.state,
      eveningLatest && eveningLatest.state,
      sun && sun.attributes.next_setting,
      pointsRaw,
      brightnessNow && brightnessNow.state,
      kelvinNow && kelvinNow.state,
    ]);

    if (cacheKey === this._cacheKey) {
      return;
    }
    this._cacheKey = cacheKey;

    this._boundaries = boundaries;
    this._phaseState = phase && phase.state;
    this._eveningEarliest = eveningEarliest;
    this._eveningLatest = eveningLatest;
    this._sun = sun;
    this._brightnessNow = brightnessNow && Number(brightnessNow.state);
    this._kelvinNow = kelvinNow && Number(kelvinNow.state);

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
    if (!this._eveningEarliest || !this._eveningLatest || !this._sun || !this._sun.attributes.next_setting) {
      return `Evening starts at ${eveningTime}.`;
    }
    const anchor = new Date(b.evening * 1000);
    const earliestTs = todayAtLocal(anchor, this._eveningEarliest.state);
    const latestTs = todayAtLocal(anchor, this._eveningLatest.state);
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

    const hourTicks = [];
    for (let h = 0; h <= 24; h += 3) {
      const t = dayStart + h * 3600;
      hourTicks.push(
        `<text x="${xOf(t).toFixed(1)}" y="${VB_H - 6}" class="axis-label" text-anchor="middle">${String(h).padStart(2, '0')}:00</text>`
      );
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

    const now = Date.now() / 1000;
    const nowInWindow = now >= dayStart && now <= dayEnd;
    const haveNow = nowInWindow && Number.isFinite(this._brightnessNow) && Number.isFinite(this._kelvinNow);
    const nowX = xOf(now).toFixed(1);
    let nowMarker = '';
    if (haveNow) {
      const nowHex = rgbToHex(kelvinToRgb(this._kelvinNow));
      nowMarker = `
        <line x1="${nowX}" y1="${PAD_TOP - 4}" x2="${nowX}" y2="${BASELINE_Y}" class="now-line" />
        <circle cx="${nowX}" cy="${(BASELINE_Y - hOf(this._brightnessNow)).toFixed(1)}" r="4" fill="${nowHex}" class="now-dot" />
      `;
    } else if (nowInWindow) {
      nowMarker = `<line x1="${nowX}" y1="${PAD_TOP - 4}" x2="${nowX}" y2="${BASELINE_Y}" class="now-line" />`;
    }

    const svg = `
      <svg viewBox="0 0 ${VB_W} ${VB_H}" preserveAspectRatio="none" class="chart">
        ${bars}
        ${boundaryLines}
        ${hourTicks.join('')}
        ${nowMarker}
        <line x1="${PAD_L}" y1="${BASELINE_Y}" x2="${VB_W - PAD_R}" y2="${BASELINE_Y}" class="axis-line" />
      </svg>
    `;

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
        .boundary-line {
          stroke: var(--secondary-text-color);
          stroke-width: 1;
          stroke-dasharray: 3 3;
          opacity: 0.6;
        }
        .boundary-label { fill: var(--primary-text-color); font-size: 11px; }
        .now-line { stroke: var(--primary-text-color); stroke-width: 1.5; }
        .now-dot { stroke: var(--card-background-color); stroke-width: 1.5; }
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
      this._tooltipEl.style.display = 'block';
      this._tooltipEl.style.left = `${clamp((clientX - rect.left), 0, rect.width - 120)}px`;
      this._tooltipEl.style.top = '4px';
      this._tooltipEl.innerHTML = `<b>${fmtTime(s.t)}</b> &nbsp; ${s.brightness} bri &nbsp; ${s.kelvin}K &nbsp; <span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${hex};vertical-align:middle;"></span>`;
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
