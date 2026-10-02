"""paper-hunter: frozen rulebook config loading."""

from config.loader import (  # noqa: F401
    DEFAULT_RULES_PATH,
    FROZEN,
    Rulebook,
    RulesError,
    load_example,
    load_rules,
    load_rules_text,
)

__all__ = [
    "DEFAULT_RULES_PATH",
    "FROZEN",
    "Rulebook",
    "RulesError",
    "load_example",
    "load_rules",
    "load_rules_text",
]