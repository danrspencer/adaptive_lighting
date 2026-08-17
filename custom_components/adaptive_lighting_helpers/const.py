DOMAIN = "adaptive_lighting_helpers"

# The optional day-phase/curve sensors and the phase-override select share
# these - see coordinator.py, sensor.py, select.py.
PHASE_OPTIONS = ["Auto", "Morning", "Day", "Evening", "Night"]

# Subentry type name for a named, additional adaptive lighting sensor -
# see config_flow.py's SensorSubentryFlow and coordinator.py's
# schedule_instances().
SUBENTRY_TYPE_SENSOR = "sensor"

# Options key for this install's own additions to the shipped list of
# bulb models needing two-step transitions - see two_step.py for why the
# list is split between a shipped default and a per-install extra.
CONF_EXTRA_TWO_STEP_MODELS = "extra_two_step_models"
