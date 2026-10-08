"""Allowed library at import, plus an environment read of its own."""

import os

from pydantic import BaseModel


class Settings(BaseModel):
    project: str | None = None


SETTINGS = Settings(project=os.environ.get("FIXTURE_APP_PROJECT"))
