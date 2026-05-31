#!/usr/bin/env python3
"""Start reindex worker with env from .env file."""
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

# Run reindex worker
sys.path.insert(0, str(Path(__file__).parent))
from scripts.reindex_worker import main
sys.exit(main())
