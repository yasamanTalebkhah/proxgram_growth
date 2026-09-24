#!/usr/bin/env python
"""CLI wrapper around src.core.seeder.

Run inside the worker container:
    docker compose exec worker python scripts/seed_tasks.py

Host runs need DATABASE_URL pointed at localhost:5432 (the .env default
points at the in-network `postgres` hostname).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.seeder import main

if __name__ == "__main__":
    raise SystemExit(main())
