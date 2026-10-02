"""CoPilot prompts: shared platform knowledge, plus one module per CoPilot.

skill.py              the shared skill.md, whole or by chapter
catalog.py            the provider catalog, for the CoPilot that picks models
agent_copilot.py      AgentCoPilot's framing and live agent/workspace state
tool_copilot.py       ToolCoPilot's framing and live tool/workspace state
task_copilot.py       TaskCoPilot's framing and live task/workspace state
"""

from __future__ import annotations

from . import (  # noqa: F401  -- bound in services.copilot.subjects
    agent_copilot,
    task_copilot,
    tool_copilot,
)
from .catalog import provider_prompt  # noqa: F401
from .skill import SKILL_PATH, sections, skill, yaml_block  # noqa: F401
