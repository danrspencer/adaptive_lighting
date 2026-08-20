/**
 * Adaptive Lighting Write Tracking — custom Lovelace card
 *
 * A UI for sensor.adaptive_lighting_write_tracking's `entities` attribute
 * (see sensor.py's _WriteTrackingSensor and write_tracking.py) - one row
 * per tracked light, showing the confirmed/pending override-protection
 * claims and a computed status. This card does no resolution logic of
 * its own beyond what the sensor already computes; its one added value
 * is "trace" - resolving a claim's raw context.id into what actually
 * happened, on demand.
 *
 * Tracing is deliberately lazy, not eager: a house-wide install can have
 * 50+ tracked lights, each with up to two claims, and eagerly resolving
 * all of them on every render would mean dozens of logbook queries per
 * card update for information nobody's looking at yet. Clicking a claim
 * fires exactly one `logbook/get_events` WebSocket call, scoped tightly
 * to that claim's own context_id and a narrow window around its
 * recorded_at timestamp (recorded_at is exactly when write_tracking.py
 * stamped this claim - see its own module docstring - so the real
 * logbook event is essentially guaranteed to fall inside a few seconds
 * either side of it). HA's logbook already knows how to walk a
 * context.id back to the automation run/service call that produced it -
 * that's the whole reason to use it here rather than reimplementing any
 * of that resolution ourselves. A claim with no recorded_at (the
 * synthetic first-write baseline - see write_tracking.py's async_record
 * docstring) can't be traced this way at all: it was never really
 * "recorded", just observed, so there's no time window to search and no
 * real event to find.
 */

const DEFAULT_ENTITY = 'sensor.adaptive_lighting_write_tracking';

const STATUS_LABEL = {
  confirmed: 'Confirmed',
  pending: 'Pending',
  mismatched: 'Mismatched',
  unavailable: 'Unavailable',
};

// Kept in the same rough "most interesting first" order a user
// debugging an override issue would actually want, without hardcoding
// entity order - lights with nothing surprising going on (confirmed)
// sink to the bottom.
const STATUS_ORDER = ['mismatched', 'pending', 'unavailable', 'confirmed'];

function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 5) return 'just now';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function friendlyOwner(ownerId) {
  if (!ownerId) return null;
  // "automation.living_room_lights_new" -> "living_room_lights_new" -
  // the domain prefix is always "automation." in practice (owner_id is
  // apply_lighting's caller-supplied string, and every real caller in
  // this project passes this.entity_id from a blueprint automation),
  // but this is display-only and falls back to the raw string for
  // anything else, so a caller using a different convention still shows
  // something sensible rather than being silently mangled.
  const dot = ownerId.indexOf('.');
  return dot === -1 ? ownerId : ownerId.slice(dot + 1);
}

function describeLogbookEntry(entry) {
  const who = entry.name || (entry.entity_id ? entry.entity_id : null) || entry.domain || 'Unknown';
  const what = entry.message ? ` ${entry.message}` : '';
  return `${who}${what}`;
}

class AdaptiveLightingWriteTrackingCard extends HTMLElement {
  static getStubConfig() {
    return { title: 'Write Tracking' };
  }

  setConfig(config) {
    this._config = config || {};
    this._entityId = this._config.entity || DEFAULT_ENTITY;
    this._cacheKey = null;
    this._filter = '';
    // entity_id|slot ("confirmed"/"pending") -> resolved logbook text,
    // or 'loading', or an Error. Cleared whenever the underlying claim's
    // own context_id changes, so a stale trace from a previous claim on
    // the same light can never be shown as if it were current.
    this._traces = new Map();
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
    }
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._entityId];
    if (!state) {
      this._renderError(`Missing entity: ${this._entityId}`);
      return;
    }
    const entities = state.attributes.entities || {};
    const cacheKey = JSON.stringify([state.state, entities, this._filter]);
    if (cacheKey === this._cacheKey) {
      return;
    }
    this._cacheKey = cacheKey;
    this._entities = entities;
    this._invalidateStaleTraces();
    this._render();
  }

  // A resolved trace is only ever valid for the exact context_id it was
  // resolved against - if the sensor's next poll/push shows a different
  // context_id in the same slot (a new write went out), any cached
  // trace text for that slot no longer describes what's actually there.
  _invalidateStaleTraces() {
    for (const key of [...this._traces.keys()]) {
      const [entityId, slot] = key.split('|');
      const claim = this._entities[entityId] && this._entities[entityId][slot];
      const cached = this._traces.get(key);
      if (!claim || cached.contextId !== claim.context_id) {
        this._traces.delete(key);
      }
    }
  }

  async _trace(entityId, slot, claim) {
    const key = `${entityId}|${slot}`;
    this._traces.set(key, { contextId: claim.context_id, state: 'loading' });
    this._render();
    try {
      const recordedAt = claim.recorded_at ? new Date(claim.recorded_at) : null;
      const startTime = recordedAt ? new Date(recordedAt.getTime() - 2000) : new Date(0);
      const endTime = recordedAt ? new Date(recordedAt.getTime() + 10000) : new Date();
      const events = await this._hass.callWS({
        type: 'logbook/get_events',
        start_time: startTime.toISOString(),
        end_time: endTime.toISOString(),
        context_id: claim.context_id,
      });
      this._traces.set(key, {
        contextId: claim.context_id,
        state: 'done',
        events: Array.isArray(events) ? events : [],
      });
    } catch (err) {
      this._traces.set(key, { contextId: claim.context_id, state: 'error', message: err && err.message ? err.message : String(err) });
    }
    this._render();
  }

  _renderError(message) {
    this.shadowRoot.innerHTML = `
      <ha-card header="${this._config.title || 'Adaptive Lighting Write Tracking'}">
        <div style="padding: 16px; color: var(--error-color, red);">${message}</div>
      </ha-card>
    `;
  }

  _claimCell(entityId, slot, claim) {
    if (!claim) {
      return `<span class="muted">—</span>`;
    }
    const owner = friendlyOwner(claim.owner_id);
    const when = relativeTime(claim.recorded_at);
    const traceKey = `${entityId}|${slot}`;
    const trace = this._traces.get(traceKey);

    let traceHtml;
    if (!claim.recorded_at) {
      traceHtml = `<span class="muted trace-note">no recorded time - can't be traced</span>`;
    } else if (!trace) {
      traceHtml = `<button class="trace-btn" data-entity="${entityId}" data-slot="${slot}">Trace</button>`;
    } else if (trace.state === 'loading') {
      traceHtml = `<span class="muted">Tracing…</span>`;
    } else if (trace.state === 'error') {
      traceHtml = `<span class="trace-error">Trace failed: ${trace.message}</span>`;
    } else if (trace.events.length === 0) {
      traceHtml = `<span class="muted">No matching logbook entry</span>`;
    } else {
      traceHtml = trace.events.map((e) => `<div class="trace-entry">${describeLogbookEntry(e)}</div>`).join('');
    }

    return `
      <div class="claim">
        <div class="owner">${owner || '<span class="muted">no owner</span>'}</div>
        <div class="meta">
          ${when ? `<span class="when">${when}</span>` : ''}
          <span class="context-id" title="${claim.context_id}">${claim.context_id.slice(0, 8)}…</span>
        </div>
        <div class="trace">${traceHtml}</div>
      </div>
    `;
  }

  _render() {
    // Captured before the DOM below gets wiped - re-rendering while the
    // user is mid-keystroke in the filter box (every keystroke triggers
    // a re-render, see the input listener below) would otherwise lose
    // focus and cursor position on every character typed.
    const previousInput = this.shadowRoot.querySelector('input.filter');
    const hadFocus = !!previousInput && this.shadowRoot.activeElement === previousInput;
    const selectionStart = hadFocus ? previousInput.selectionStart : null;

    const entities = this._entities || {};
    const filter = this._filter.trim().toLowerCase();
    const rows = Object.entries(entities)
      .filter(([entityId, record]) => {
        if (!filter) return true;
        const haystack = `${entityId} ${(record.confirmed && record.confirmed.owner_id) || ''} ${(record.pending && record.pending.owner_id) || ''}`.toLowerCase();
        return haystack.includes(filter);
      })
      .sort(([aId, a], [bId, b]) => {
        const order = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
        return order !== 0 ? order : aId.localeCompare(bId);
      });

    const bodyRows = rows
      .map(([entityId, record]) => {
        const friendly = (this._hass.states[entityId] && this._hass.states[entityId].attributes.friendly_name) || entityId;
        return `
          <tr>
            <td class="light-cell" data-entity="${entityId}">
              <div class="light-name">${friendly}</div>
              <div class="light-id muted">${entityId}</div>
            </td>
            <td><span class="status-badge status-${record.status}">${STATUS_LABEL[record.status] || record.status}</span></td>
            <td>${this._claimCell(entityId, 'confirmed', record.confirmed)}</td>
            <td>${this._claimCell(entityId, 'pending', record.pending)}</td>
          </tr>
        `;
      })
      .join('');

    const count = rows.length;
    const total = Object.keys(entities).length;
    const countLabel = filter && count !== total ? `${count} of ${total} lights` : `${total} light${total === 1 ? '' : 's'}`;

    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .card-content { padding: 8px 16px 16px; }
        .toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-bottom: 8px;
        }
        .count { font-size: 0.85em; color: var(--secondary-text-color); white-space: nowrap; }
        input.filter {
          flex: 1;
          min-width: 0;
          padding: 6px 10px;
          border-radius: 8px;
          border: 1px solid var(--divider-color, #888);
          background: var(--card-background-color);
          color: var(--primary-text-color);
          font-size: 0.9em;
        }
        table { width: 100%; border-collapse: collapse; font-size: 0.88em; }
        th {
          text-align: left;
          font-size: 0.78em;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: var(--secondary-text-color);
          padding: 4px 8px;
          border-bottom: 1px solid var(--divider-color, #888);
        }
        td { padding: 8px; border-bottom: 1px solid var(--divider-color, #888); vertical-align: top; }
        tr:last-child td { border-bottom: none; }
        .light-cell { cursor: pointer; }
        .light-name { font-weight: 500; }
        .light-id, .muted { color: var(--secondary-text-color); }
        .light-id { font-size: 0.85em; }
        .status-badge {
          display: inline-block;
          padding: 2px 8px;
          border-radius: 10px;
          font-size: 0.85em;
          white-space: nowrap;
        }
        .status-confirmed { background: var(--success-color, #43a047); color: white; }
        .status-pending { background: var(--warning-color, #ffa726); color: white; }
        .status-mismatched { background: var(--error-color, #e53935); color: white; }
        .status-unavailable { background: var(--disabled-color, #9e9e9e); color: white; }
        .claim { min-width: 160px; }
        .owner { font-weight: 500; }
        .meta { display: flex; gap: 8px; align-items: baseline; font-size: 0.82em; color: var(--secondary-text-color); }
        .context-id { font-family: var(--code-font-family, monospace); }
        .trace { margin-top: 4px; font-size: 0.85em; }
        .trace-btn {
          background: none;
          border: 1px solid var(--divider-color, #888);
          border-radius: 6px;
          color: var(--primary-color);
          padding: 2px 8px;
          font-size: 0.85em;
          cursor: pointer;
        }
        .trace-btn:hover { background: var(--secondary-background-color, rgba(127,127,127,0.1)); }
        .trace-error { color: var(--error-color, red); }
        .trace-entry { padding: 2px 0; }
        .empty { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
      </style>
      <ha-card header="${this._config.title || 'Adaptive Lighting Write Tracking'}">
        <div class="card-content">
          <div class="toolbar">
            <input class="filter" type="text" placeholder="Filter by light or automation…" value="${this._filter}" />
            <span class="count">${countLabel}</span>
          </div>
          ${
            rows.length === 0
              ? `<div class="empty">${total === 0 ? 'No lights tracked yet.' : 'No lights match this filter.'}</div>`
              : `
            <table>
              <thead>
                <tr>
                  <th>Light</th>
                  <th>Status</th>
                  <th>Confirmed</th>
                  <th>Pending</th>
                </tr>
              </thead>
              <tbody>${bodyRows}</tbody>
            </table>
          `
          }
        </div>
      </ha-card>
    `;

    const filterInput = this.shadowRoot.querySelector('input.filter');
    filterInput.addEventListener('input', (ev) => {
      this._filter = ev.target.value;
      this._cacheKey = null; // force _render on next hass tick even if state itself hasn't changed
      this._render();
    });
    if (hadFocus) {
      filterInput.focus();
      if (selectionStart != null) {
        filterInput.setSelectionRange(selectionStart, selectionStart);
      }
    }

    this.shadowRoot.querySelectorAll('.trace-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const entityId = btn.dataset.entity;
        const slot = btn.dataset.slot;
        const claim = this._entities[entityId][slot];
        this._trace(entityId, slot, claim);
      });
    });

    this.shadowRoot.querySelectorAll('.light-cell').forEach((cell) => {
      cell.addEventListener('click', () => {
        this.dispatchEvent(
          new CustomEvent('hass-more-info', {
            detail: { entityId: cell.dataset.entity },
            bubbles: true,
            composed: true,
          })
        );
      });
    });
  }
}

customElements.define('adaptive-lighting-write-tracking-card', AdaptiveLightingWriteTrackingCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'adaptive-lighting-write-tracking-card',
  name: 'Adaptive Lighting Write Tracking',
  description: 'Per-light override-protection claims, with trace-back to what actually happened.',
});
