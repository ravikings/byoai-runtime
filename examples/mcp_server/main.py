"""Example: ByoAI Runtime exposed as an MCP tool server.

Run:

    pip install 'byoai-runtime[mcp]'
    export OPENAI_API_KEY=sk-...          # or point BYOAI_BASE_URL at Ollama/vLLM
    python examples/mcp_server/main.py --http    # streamable HTTP on :8000/mcp
    python examples/mcp_server/main.py           # stdio (for Claude Desktop, etc.)

Any MCP client can then call the ``execute`` tool with the same payload
dialect every other ByoAI transport uses: ``{"input": "..."}``.
"""

from __future__ import annotations

import asyncio
import os
import sys

from byoai import Runtime
from byoai.integrations.mcp import create_server

llm_config: dict = {
    # BYOAI_BASE_URL implies an OpenAI-wire-format endpoint (Ollama/vLLM/a
    # gateway) unless BYOAI_PROVIDER was set explicitly — e.g. AnthropicProvider
    # also takes base_url, so don't clobber an explicit non-OpenAI-compat choice.
    "provider": os.environ.get(
        "BYOAI_PROVIDER", "openai_compatible" if os.environ.get("BYOAI_BASE_URL") else "openai"
    ),
    "model": os.environ.get("BYOAI_MODEL", "gpt-4o-mini"),
}
if os.environ.get("BYOAI_BASE_URL"):
    llm_config["base_url"] = os.environ["BYOAI_BASE_URL"]

runtime = Runtime(llm=llm_config, cache={"provider": "memory"})
server = create_server(runtime, name="byoai-example")

if __name__ == "__main__":
    if "--http" in sys.argv:
        asyncio.run(server.run_streamable_http_async(port=int(os.environ.get("PORT", "8000"))))
    else:
        asyncio.run(server.run_stdio_async())
