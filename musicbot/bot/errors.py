"""Shared by the settings code and the message format."""
from __future__ import annotations

import re

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ConfigError(ValueError):
    """A message the dashboard shows next to the field."""
