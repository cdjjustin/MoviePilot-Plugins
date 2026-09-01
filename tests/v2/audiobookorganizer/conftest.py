"""测试环境：mock MoviePilot 宿主依赖。"""

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _ensure_mock(name: str) -> ModuleType:
    if name not in sys.modules:
        sys.modules[name] = ModuleType(name)
    return sys.modules[name]


# Mock app.plugins._PluginBase
app = _ensure_mock("app")
app_plugins = _ensure_mock("app.plugins")
app_plugins._PluginBase = type(  # type: ignore[attr-defined]
    "_PluginBase",
    (),
    {
        "get_data": lambda self, key: None,
        "save_data": lambda self, key, val: None,
        "post_message": lambda self, **kw: None,
    },
)

# Mock app.schemas.types
app_schemas = _ensure_mock("app.schemas")
app_schemas_types = _ensure_mock("app.schemas.types")
app_schemas_types.NotificationType = MagicMock(Manual="manual")

# Mock app.sdk / app.core logging
for mod in ("app.sdk", "app.sdk.logging", "app.core.config", "app.log"):
    _ensure_mock(mod)
sys.modules["app.sdk.logging"].logger = MagicMock()
sys.modules["app.log"].logger = MagicMock()

# fastapi is a real dependency for type hints in __init__.py; install if missing
try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi_mock = _ensure_mock("fastapi")
    fastapi_mock.HTTPException = type("HTTPException", (Exception,), {})
