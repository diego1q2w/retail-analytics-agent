"""The investigation agent's Pydantic AI integration, shared by every runtime.

Runtime-neutral: nothing here imports Temporal (or Pydantic AI's durable
execution), so the same agent, guarded model and permission-filtered toolset
run under the Temporal workflow and without it.
"""
