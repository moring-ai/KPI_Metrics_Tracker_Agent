"""The MCP servers. These processes hold the credentials; the KPI client does not.

Nothing in kpi_tracker/ may import this package -- that separation is the whole
point, and tests/test_import_boundary.py enforces it.
"""
