DOMAIN = "adaptive_lighting_helpers"

# The optional day-phase/curve sensors and the phase-override select share
# these - see coordinator.py, sensor.py, select.py.
PHASE_OPTIONS = ["Auto", "Morning", "Day", "Evening", "Night"]
PHASE_OVERRIDE_ENTITY_ID = "select.adaptive_lighting_phase"
