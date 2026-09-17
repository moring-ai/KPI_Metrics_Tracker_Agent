"""The AgentCore deployment of the KPI tracker.

Thin by design: all the logic lives in the `kpi_tracker` package at the repo
root, which has 367 tests. This folder is only the AICP-shaped wrapper around
it, so the two deployments cannot drift.
"""
