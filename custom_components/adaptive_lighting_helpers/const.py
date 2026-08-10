DOMAIN = "adaptive_lighting_helpers"

# The optional day-phase/curve sensors and the phase-override select share
# these - see coordinator.py, sensor.py, select.py.
PHASE_OPTIONS = ["Auto", "Morning", "Day", "Evening", "Night"]

# Subentry type name for a named, additional adaptive lighting sensor -
# see config_flow.py's SensorSubentryFlow and coordinator.py's
# schedule_instances().
SUBENTRY_TYPE_SENSOR = "sensor"
