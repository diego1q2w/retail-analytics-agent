"""The local execution backend: investigations as tasks of the API process.

Runtime-neutral: nothing here imports Temporal. Work survives client
disconnects while the process lives; it is interrupted, never replayed, when
the process stops or dies.
"""
