"""Test environment isolation.

litellm calls load_dotenv() at import time (LITELLM_MODE defaults to DEV),
which leaks the real .env into os.environ and silently changes what
``Settings(_env_file=None)`` sees. Set PRODUCTION before any test module
imports the bot package so tests always run against clean defaults.
"""
import os

os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
