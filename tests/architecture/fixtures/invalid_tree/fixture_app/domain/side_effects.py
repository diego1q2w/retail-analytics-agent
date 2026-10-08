"""Environment read and file access at import time."""

import os

PROJECT = os.environ.get("FIXTURE_PROJECT")
with open(os.devnull) as handle:
    handle.read()
