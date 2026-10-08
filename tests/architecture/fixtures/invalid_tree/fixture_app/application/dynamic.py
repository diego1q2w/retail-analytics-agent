"""Dynamic import that would bypass static checks."""

import importlib

driver = importlib.import_module("json")
