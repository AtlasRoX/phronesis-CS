"""Environment settings: the declarative registry, typed accessors and startup validation."""

from .settings import (
    EXTERNAL_NAMES,
    GROUPS,
    REGISTRY,
    Setting,
    get_bool,
    get_float,
    get_int,
    get_list,
    get_path,
    get_str,
    validate_environment,
)

__all__ = [
    "EXTERNAL_NAMES",
    "GROUPS",
    "REGISTRY",
    "Setting",
    "get_bool",
    "get_float",
    "get_int",
    "get_list",
    "get_path",
    "get_str",
    "validate_environment",
]
