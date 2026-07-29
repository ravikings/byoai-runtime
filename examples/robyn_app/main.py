"""Example: ByoAI Runtime on Robyn (Rust-powered HTTP).

Run:

    pip install 'byoai-runtime[robyn]'
    export OPENAI_API_KEY=sk-...          # or point BYOAI_BASE_URL at Ollama/vLLM
    python examples/robyn_app/main.py --port 8080

Try it (same transport dialect as the FastAPI integration):

    curl -s localhost:8080/byoai/execute -X POST -H 'content-type: application/json' \
        -d '{"input": "What is an execution runtime?"}'

    curl -N localhost:8080/byoai/stream -X POST -H 'content-type: application/json' \
        -d '{"input": "Stream me a haiku about runtimes"}'
"""

from __future__ import annotations

import os

from byoai import Runtime
from byoai.integrations.robyn import create_app

llm_config: dict = {
    "provider": os.environ.get("BYOAI_PROVIDER", "openai"),
    "model": os.environ.get("BYOAI_MODEL", "gpt-4o-mini"),
}
if os.environ.get("BYOAI_BASE_URL"):
    llm_config["provider"] = "openai_compatible"
    llm_config["base_url"] = os.environ["BYOAI_BASE_URL"]

runtime = Runtime(llm=llm_config, cache={"provider": "memory"})
app = create_app(runtime)

if __name__ == "__main__":
    app.start(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
