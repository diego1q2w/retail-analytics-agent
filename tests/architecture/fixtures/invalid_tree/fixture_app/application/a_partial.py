"""Environment read hidden inside a callback that pydantic runs."""

import functools
import os

from pydantic import BaseModel, Field


class Partial(BaseModel):
    token: str | None = Field(
        default_factory=functools.partial(os.environ.get, "SECRET_TOKEN")
    )


PARTIAL = Partial()
