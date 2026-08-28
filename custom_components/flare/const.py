DOMAIN = "flare"

# The optional day-phase/curve sensors and the phase-override select share
# these - see coordinator.py, sensor.py, select.py.
PHASE_OPTIONS = ["Auto", "Morning", "Day", "Evening", "Night"]

# Subentry type name for a named, additional adaptive lighting sensor -
# see config_flow.py's SensorSubentryFlow and coordinator.py's
# schedule_instances().
SUBENTRY_TYPE_SENSOR = "sensor"

# Options key for the list of bulb models needing two-step transitions.
# Holds the whole list, not additions to a hidden one - the field is
# pre-populated with DEFAULT_TWO_STEP_MODEL_PATTERNS so what ships and
# what you add are the same editable thing (see two_step.py).
CONF_TWO_STEP_MODELS = "two_step_models"

# Subentry type for a state device - a named tracking scope that owns
# the override-protection claims for whatever lights its target covers.
# See write_tracking.py for the model and coordinator.py's
# state_instances() for how they're enumerated.
SUBENTRY_TYPE_STATE = "state"

# The state subentry's target: an area/device/entity selector, resolved
# per light to decide which scope tracks it.
CONF_TARGET = "target"

# Fired once each time a tracked light passes into "overridden" - edge
# triggered, not level, so it marks the transition rather than repeating
# while the light stays taken. Carries a full snapshot of both claims and
# the live values at that instant, which is the thing you can't
# reconstruct afterwards: by the time anyone looks, the curve has moved
# and the evidence is gone. See sensor.py's _refresh_statuses.
EVENT_LIGHT_OVERRIDDEN = "flare_light_overridden"

# This integration installs as two separate config entries rather than
# one, so the integration page shows two top-level blocks - schedules and
# tracking - instead of flattening both kinds of thing into one list of
# sibling subentries. Grouping subentries by type isn't possible: HA's
# frontend renders one section per subentry with no hook to group them,
# so the entry is the only level where this can be expressed.
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_SCHEDULES = "schedules"
ENTRY_TYPE_TRACKING = "tracking"
