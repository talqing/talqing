"""Python client for the Talqing API.

```python
from talqing import Talqing

with Talqing.from_env() as talqing:
    agent = talqing.agents.get(agent_id)
```

The API surface under `talqing.agents`, `talqing.calls` and the rest is
generated from `openapi/openapi.json` — it is the same set of operations, under
the same names, as the TypeScript SDK. Every request and response shape is
exported from here too, as a TypedDict.
"""

from ._transport import OMIT, AsyncStream, Stream
from ._version import __version__
from .client import AsyncTalqing, Talqing, paginate, paginate_async
from .errors import TalqingAPIError
from .gen.types import *  # noqa: F403
from .gen.types import __all__ as _types

__all__ = [
    "AsyncStream",
    "AsyncTalqing",
    "OMIT",
    "Stream",
    "Talqing",
    "TalqingAPIError",
    "__version__",
    "paginate",
    "paginate_async",
    *_types,
]
