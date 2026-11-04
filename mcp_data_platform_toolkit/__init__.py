"""
Data Platform Toolkit: an MCP server for Power BI projects saved as PBIP.

It reads, analyzes and safely edits the TMDL semantic model, finds where
measures and columns are used in the PBIR reports, and traces the lineage
from the SQL Server database project to the report visuals.

Only the version lives here, in syntax any Python can read: `server.py`
checks the interpreter version, and this module is imported before it.
"""

# Declared to MCP clients in the handshake, printed by --check, and read by
# the build as the package version.
__version__ = "1.5.1"
