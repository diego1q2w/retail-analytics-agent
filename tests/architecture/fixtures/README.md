Deliberately invalid package used to prove the architecture checks fail.
Never import it from application code; ruff, mypy and pytest collection skip it.

`invalid_tree/fixture_app/adapters/agent/` stands in for runtime-neutral agent
code that imports Temporal directly (`durable`), through the project's Temporal
adapter (`wired`) or only transitively through a helper (`transitive`).
