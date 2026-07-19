"""Smart Group Bot package bootstrap."""

from __future__ import annotations

import os

# LiteLLM otherwise performs a blocking network fetch for its model-cost map
# during import.  Several modules import LiteLLM before ``services.llm`` is
# loaded, so this must be set at package bootstrap rather than in one service.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
