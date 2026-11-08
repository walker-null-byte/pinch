`coding_mcp_server.py` is an unmodified copy of
https://github.com/walker-null-byte/coding-tools at commit 23b2501af887fe391b18da2c47876cffeab81a0f (MIT).

coding-tools is the pinch author's own library and existed before the hackathon. It is a single
file with no packaging (no pyproject.toml), so it cannot be installed with pip or uv. pinch ships a copy
and loads it in-process. To use a different copy, set PINCH_CODING_TOOLS=/path/to/coding_mcp_server.py.
