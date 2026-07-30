"""Transport integrations wiring a :class:`byoai.Runtime` into web frameworks.

Each submodule requires its extra: ``byoai.integrations.fastapi``
(``pip install byoai-runtime[fastapi]``), ``byoai.integrations.robyn``
(``[robyn]``), and ``byoai.integrations.mcp`` (``[mcp]``). Nothing is imported
eagerly here so the core package stays dependency-free.
"""
