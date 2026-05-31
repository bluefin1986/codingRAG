#!/usr/bin/env python3
"""Start codingRAG API with env from .env file."""
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Start uvicorn
import uvicorn
uvicorn.run("api.app:app", host="0.0.0.0", port=8060)
