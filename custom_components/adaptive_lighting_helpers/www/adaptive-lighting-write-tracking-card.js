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
  controlled: 'Controlled',
  pending: 'Pending',
  overridden: 'Overridden',
  unavailable: 'Unavailable',
  off: 'Off',
};

// Shown as hover text on each status badge - what the word actually
// means, since "Controlled" vs "Overridden" isn't self-explanatory
// without knowing how the confirmed/pending mechanism behind it works.
const STATUS_TOOLTIP = {
  controlled: 'This light is under adaptive control - its current value matches a write we made (or, even if its ' +
    'reported context differs, still matches what we last asked for). It will be updated normally on the next tick.',
  pending: "Our most recent write hasn't been reconfirmed by a later tick yet - still effectively under control, " +
    'just not yet double-checked.',
  overridden: "Something else changed this light since our last write, and its current value doesn't match what " +
    'we asked for either - left alone until it turns off, goes unavailable and reconnects, or you force an update.',
  unavailable: "This light isn't reporting a state right now (offline, unreachable, or reconnecting).",
  off: "This light is currently off - override protection doesn't apply to it. It'll be freely managed the next " +
    'time it turns on.',
};

// Short, human explanation of matched_via - only meaningful for
// pending/controlled (classify() returns null otherwise, nothing to
// explain). Shown as a small inline annotation next to the status
// badge, so "why" is visible without needing to hover - the tooltip
// text alone was easy to miss.
const MATCHED_VIA_LABEL = {
  context: 'context matched',
  value: 'colour/brightness matched',
};

const MATCHED_VIA_TOOLTIP = {
  context: "The light's own reported context.id is exactly the one we wrote it with - a direct, unambiguous match.",
  value: "The light's reported context.id doesn't match anything we recorded, but its current brightness/colour " +
    'still matches exactly what we last asked for - almost certainly the device\'s own delayed confirmation ' +
    "landing under a new context (Home Assistant only reuses our context.id for up to 5 seconds), not a real touch.",
};

// Kept in the same rough "most interesting first" order a user
// debugging an override issue would actually want, without hardcoding
// entity order - lights with nothing surprising going on (controlled)
// sink to the bottom, and off lights sink lowest of all (more inert
// even than controlled - nothing is being actively managed at all).
const STATUS_ORDER = ['overridden', 'pending', 'unavailable', 'controlled', 'off'];

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
    // Per-entity state for the "Clear" action - a Set of entity_ids
    // currently mid-call (disables/relabels that row's button) and a
    // Map of entity_id -> error message for a call that failed, shown
    // inline in that row rather than displacing the whole card the way
    // _renderError does (see _clear's own docstring-equivalent comment).
    this._clearing = new Set();
    this._clearErrors = new Map();
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

  async _clear(entityId) {
    // No confirmation prompt - clearing just lets the light get
    // updated normally again on the next tick, from anyone. Not a
    // destructive action worth gating (unlike, say, deleting data):
    // the record is a diagnostic bookkeeping entry, not the light
    // itself, and a fresh one gets re-established the moment anything
    // next writes to this entity.
    this._clearing.add(entityId);
    this._clearErrors.delete(entityId);
    this._render();
    try {
      await this._hass.callService('adaptive_lighting_helpers', 'clear_ownership', { entities: [entityId] });
      // No manual re-render needed on success - clear_ownership fires
      // SIGNAL_WRITE_TRACKING_UPDATED server-side, which pushes a fresh
      // sensor state and re-enters `set hass` normally, same as every
      // other change this card reacts to. The entity may vanish from
      // `entities` entirely on that next push if it had no live state -
      // _clearing/_clearErrors keyed by entity_id just go unused then,
      // not stale-referenced.
    } catch (err) {
      this._clearErrors.set(entityId, err && err.message ? err.message : String(err));
    } finally {
      this._clearing.delete(entityId);
      this._render();
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
        const haystack = `${entityId} ${record.owner_id || ''} ${(record.confirmed && record.confirmed.owner_id) || ''} ${(record.pending && record.pending.owner_id) || ''}`.toLowerCase();
        return haystack.includes(filter);
      })
      .sort(([aId, a], [bId, b]) => {
        const order = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
        return order !== 0 ? order : aId.localeCompare(bId);
      });

    const bodyRows = rows
      .map(([entityId, record]) => {
        const friendly = (this._hass.states[entityId] && this._hass.states[entityId].attributes.friendly_name) || entityId;
        const owner = friendlyOwner(record.owner_id);
        // Only link the owner when it resolves to a real, currently-live
        // entity - owner_id is a free-text field for callers other than
        // the blueprint (which always passes a real this.entity_id), and
        // an entity can also have been removed since the claim was made.
        const ownerIsEntity = !!(record.owner_id && this._hass.states[record.owner_id]);
        const ownerCell = owner
          ? ownerIsEntity
            ? `<span class="owner-cell owner-link" data-entity="${record.owner_id}">${owner}</span>`
            : `<span class="owner-cell">${owner}</span>`
          : '<span class="muted">—</span>';
        const matchedViaLabel = MATCHED_VIA_LABEL[record.matched_via];
        const clearing = this._clearing.has(entityId);
        const clearError = this._clearErrors.get(entityId);
        return `
          <tr>
            <td class="light-cell" data-entity="${entityId}">
              <div class="light-name">${friendly}</div>
              <div class="light-id muted">${entityId}</div>
            </td>
            <td>
              <span class="status-badge status-${record.status}" title="${STATUS_TOOLTIP[record.status] || ''}">${STATUS_LABEL[record.status] || record.status}</span>
              ${matchedViaLabel ? `<div class="matched-via muted" title="${MATCHED_VIA_TOOLTIP[record.matched_via] || ''}">${matchedViaLabel}</div>` : ''}
            </td>
            <td>${ownerCell}</td>
            <td>${this._claimCell(entityId, 'confirmed', record.confirmed)}</td>
            <td>${this._claimCell(entityId, 'pending', record.pending)}</td>
            <td>
              <button class="clear-btn" data-entity="${entityId}" ${clearing ? 'disabled' : ''}>${clearing ? 'Clearing…' : 'Clear'}</button>
              ${clearError ? `<div class="trace-error">${clearError}</div>` : ''}
            </td>
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
          cursor: help;
        }
        .status-controlled { background: var(--success-color, #43a047); color: white; }
        .status-pending { background: var(--warning-color, #ffa726); color: white; }
        .status-overridden { background: var(--error-color, #e53935); color: white; }
        .status-unavailable { background: var(--disabled-color, #9e9e9e); color: white; }
        .status-off { background: var(--state-icon-off-color, #44739e); color: white; }
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
        .trace-error { color: var(--error-color, red); font-size: 0.85em; margin-top: 4px; max-width: 160px; }
        .trace-entry { padding: 2px 0; }
        .owner-cell { font-weight: 500; }
        .owner-link { cursor: pointer; text-decoration: underline dotted; text-underline-offset: 2px; }
        .owner-link:hover { color: var(--primary-color); }
        .matched-via { font-size: 0.78em; margin-top: 2px; cursor: help; }
        .clear-btn {
          background: none;
          border: 1px solid var(--error-color, #e53935);
          border-radius: 6px;
          color: var(--error-color, #e53935);
          padding: 2px 8px;
          font-size: 0.85em;
          cursor: pointer;
          white-space: nowrap;
        }
        .clear-btn:hover { background: var(--error-color, #e53935); color: white; }
        .clear-btn:disabled { opacity: 0.6; cursor: default; }
        .clear-btn:disabled:hover { background: none; color: var(--error-color, #e53935); }
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
                  <th title="Whether this light is currently under adaptive control - see each status badge for what it means.">Status</th>
                  <th title="Whichever claim (confirmed or pending) currently matches this light - who's in control of it right now. Blank when nothing currently matches (off/unavailable/unclaimed/overridden).">Owner</th>
                  <th title="The last write to this light that a later tick actually observed landing.">Confirmed</th>
                  <th title="The most recent write attempted for this light, not yet reconfirmed by a later tick.">Pending</th>
                  <th title="Manually discard this light's tracked record entirely - the escape hatch for a light stuck Overridden with no other way back.">Actions</th>
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

    this.shadowRoot.querySelectorAll('.clear-btn').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation(); // don't also trigger the row's light-cell -> hass-more-info click
        this._clear(btn.dataset.entity);
      });
    });

    // Both the light cell and a resolved owner link open the same
    // more-info dialog, just for whichever entity_id they carry in
    // data-entity - one handler covers both rather than duplicating it.
    this.shadowRoot.querySelectorAll('.light-cell, .owner-link').forEach((cell) => {
      cell.addEventListener('click', (ev) => {
        ev.stopPropagation();
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
