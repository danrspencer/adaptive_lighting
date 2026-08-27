DOMAIN = "adaptive_lighting_helpers"

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

# Options key for the optional per-owner count sensors (sensor.py's
# _OwnerCountSensor). Off by default: they're derived entirely from data
# the global write-tracking sensor already exposes, so enabling them is a
# choice about how many entities you want, not about what's tracked.
CONF_OWNER_SENSORS = "owner_sensors"
