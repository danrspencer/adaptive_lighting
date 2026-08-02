"""Pure-Python core for adaptive lighting: no Home Assistant/pyscript
dependency, safe to `import adaptive_lighting` from either pytest or a
pyscript app."""

from .curve import brightness_for_phase, kelvin_for_phase, phase_at
from .grouping import EntityLookup, Group, build_groups

__all__ = [
    "phase_at",
    "brightness_for_phase",
    "kelvin_for_phase",
    "EntityLookup",
    "Group",
    "build_groups",
]
