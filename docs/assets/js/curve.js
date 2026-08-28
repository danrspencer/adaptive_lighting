/**
 * Browser port of custom_components/flare/curve.py.
 *
 * This exists so the docs site's interactive playground can recompute the
 * schedule live as you drag a slider. The real Lovelace card never does this
 * - it reads a precomputed `points` attribute off the sensor, which the
 * integration fills in from curve.py itself (see coordinator.py's
 * _compute_curve_points). So this file feeds the *real* card the same shape
 * of data a live Home Assistant would, rather than reimplementing the card.
 *
 * Being a second implementation of the schedule, it can drift from curve.py.
 * tests/test_curve_js_parity.py runs this module under node against a grid
 * of inputs and asserts every value matches curve.py exactly, so drift fails
 * CI rather than silently leaving the docs graph wrong.
 *
 * Two different rounding rules are deliberate, not an inconsistency:
 *   - brightness/Kelvin use pyRound (half-to-EVEN), because curve.py rounds
 *     them with Python's built-in round(), which is banker's rounding.
 *   - kelvinToRgb uses Math.round (half-UP), because curve.py rounds *it*
 *     with floor(x + 0.5) specifically to match the dashboard card's JS.
 * Getting either backwards produces off-by-one values on exact .5 ties only
 * - invisible by eye, caught by the parity test.
 */

// Ported verbatim from curve.py's module-level constants. Kept as the same
// names so a diff between the two files lines up.
export const DEFAULT_CURVE_VALUES = {
  morning_brightness: 255,
  morning_kelvin: 6667,
  day_brightness: 255,
  day_end_kelvin: 4000,
  evening_brightness: 180,
  evening_kelvin: 3200,
  night_brightness: 80,
  night_kelvin: 2700,
};

export const DEFAULT_SCHEDULE_HOURS = {
  morning: 6,
  day: 8,
  evening_earliest: 17,
  evening_latest: 20,
  night: 22,
};

function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), hi);
}

/** Python's round(): half-to-even. NOT the same as JS Math.round(). */
function pyRound(x) {
  const f = Math.floor(x);
  const diff = x - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/**
 * Tanner Helland's Kelvin -> RGB approximation. Matches curve.py's
 * kelvin_to_rgb() and the card's own kelvinToRgb() - all three agree,
 * including on .5 ties (see this file's header).
 */
export function kelvinToRgb(kelvin) {
  const temp = kelvin / 100;
  let r, g, b;

  r = temp <= 66 ? 255 : clamp(329.698727446 * Math.pow(temp - 60, -0.1332047592), 0, 255);

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

/** Which phase an instant falls in. Mirrors curve.py's phase_at(). */
export function phaseAt(t, morningTs, dayStartTs, eveningTs, nightTs) {
  if (t < morningTs) return 'Night';
  if (t < dayStartTs) return 'Morning';
  if (t < eveningTs) return 'Day';
  if (t < nightTs) return 'Evening';
  return 'Night';
}

/** Mirrors curve.py's brightness_for_phase(). */
export function brightnessForPhase(dayPhase, nowTs, nightTs, v = DEFAULT_CURVE_VALUES) {
  if (dayPhase === 'Morning') return v.morning_brightness;
  if (dayPhase === 'Day') return v.day_brightness;
  if (dayPhase === 'Evening') {
    const fadeStartTs = nightTs - 3600;
    if (nowTs < fadeStartTs) return v.evening_brightness;
    const t = (nightTs - nowTs) / 3600;
    // 1.6x the evening->night span, not the span itself - see curve.py's
    // comment. Kept as a ratio so a custom brightness range keeps the shape.
    const b = v.night_brightness + (v.evening_brightness - v.night_brightness) * 1.6 * t;
    const [lo, hi] = [v.night_brightness, v.evening_brightness].sort((x, y) => x - y);
    return pyRound(clamp(b, lo, hi));
  }
  return v.night_brightness; // Night
}

/** Mirrors curve.py's kelvin_for_phase(), including every ramp's clamp. */
export function kelvinForPhase(dayPhase, nowTs, eveningTs, dayStartTs, nightTs, v = DEFAULT_CURVE_VALUES) {
  if (dayPhase === 'Morning') return v.morning_kelvin;

  if (dayPhase === 'Day') {
    const totalDay = eveningTs - dayStartTs;
    let tDay = totalDay > 0 ? (nowTs - dayStartTs) / totalDay : 0;
    tDay = clamp(tDay, 0, 1);
    return pyRound(v.morning_kelvin - (v.morning_kelvin - v.day_end_kelvin) * tDay);
  }

  if (dayPhase === 'Evening') {
    const fadeStartTs = nightTs - 3600;
    if (nowTs >= fadeStartTs) {
      // Clamped because dayPhase is a parameter, not derived from nowTs -
      // "Evening" can legitimately be asked for past nightTs (a manual phase
      // override). Unclamped this extrapolates straight through nightKelvin.
      const t = clamp((nightTs - nowTs) / 3600, 0, 1);
      return pyRound(v.night_kelvin + (v.evening_kelvin - v.night_kelvin) * t);
    }
    const holdStartTs = Math.min(eveningTs + 3600, fadeStartTs);
    if (nowTs < holdStartTs) {
      const rampLen = holdStartTs - eveningTs;
      const t = clamp(rampLen > 0 ? (nowTs - eveningTs) / rampLen : 1, 0, 1);
      return pyRound(v.day_end_kelvin - (v.day_end_kelvin - v.evening_kelvin) * t);
    }
    return v.evening_kelvin;
  }

  return v.night_kelvin; // Night
}

/** Mirrors curve.py's targets_for_phase(). */
export function targetsForPhase(dayPhase, nowTs, eveningTs, dayStartTs, nightTs, v = DEFAULT_CURVE_VALUES) {
  const brightness = brightnessForPhase(dayPhase, nowTs, nightTs, v);
  const kelvin = kelvinForPhase(dayPhase, nowTs, eveningTs, dayStartTs, nightTs, v);
  return { brightness, kelvin, rgb_color: kelvinToRgb(kelvin) };
}

/**
 * The Evening boundary clamp, mirroring coordinator.py's _compute_boundaries.
 * Evening is the one boundary that tracks the sun, bounded either side so a
 * 4pm winter sunset doesn't start Evening mid-afternoon and a 10pm midsummer
 * one doesn't mean it never really arrives.
 */
export function eveningTsFor(sunsetTs, earliestTs, latestTs) {
  return Math.max(earliestTs, Math.min(sunsetTs, latestTs));
}

/**
 * The 289-point, every-5-minutes day curve - the same shape and count
 * coordinator.py's _compute_curve_points() puts on the sensor's `points`
 * attribute, which is what the real card reads.
 */
export function buildPoints(midnightTs, boundaries, v = DEFAULT_CURVE_VALUES) {
  const { morningTs, dayStartTs, eveningTs, nightTs } = boundaries;
  const points = [];
  for (let i = 0; i < 289; i++) {
    const t = midnightTs + i * 300;
    const phase = phaseAt(t, morningTs, dayStartTs, eveningTs, nightTs);
    const targets = targetsForPhase(phase, t, eveningTs, dayStartTs, nightTs, v);
    points.push({ t: Math.trunc(t), brightness: targets.brightness, kelvin: targets.kelvin });
  }
  return points;
}
