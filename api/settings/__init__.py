"""Settings API — router + constants + shared names.

Split from api/settings.py (1128 lines) into api/settings/{read,write,rules,
provider,probe}.py. This module re-exports config._config_path so tests
monkeypatching api.settings._config_path keep working — consumers resolve
it at runtime via 'from api.settings import _config_path'.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter

from config import _config_path

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

# Allowed protocols / provider-name charset (shared by write + probe).
_ALLOWED_PROTOCOLS = {"openai", "anthropic"}
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9_-]{2,32}$")


# Import submodules AFTER router/constants exist.
# fmt: off
from api.settings import (  # noqa: E402,F401
    read,
    write,
    rules,
    provider,
    probe,
)
# fmt: on

# Re-export the public surface tests import from api.settings directly.
_mask = read._mask
_settings_config_path = read._settings_config_path
