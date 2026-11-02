# Installation

[Back to the README](../README.md)

## Requirements

- Python 3.10 or later, on Windows, Linux or macOS: the server is pure Python and CI tests it on Windows and Linux. Only the optional Microsoft bridge needs Windows.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) is the easiest way to install and run it; `pip` works too.
- A Power BI project saved as **PBIP** with the **TMDL** semantic model format (and, for report analysis, the **PBIR** report format)
- For the lineage of the data warehouse: a **SQL Server database project** (`.sqlproj`, SSDT or SDK-style) with its `.sql` files

## Install and check

With [uv](https://docs.astral.sh/uv/) there is nothing to install by hand: `uvx` downloads the server the first time, keeps it in its cache and runs it. Check that it works:

```bash
uvx --python 3.13 mcp-data-platform-toolkit --check
uvx --python 3.13 mcp-data-platform-toolkit --check "C:\path\to\Project.pbip"   # also validates a project
```

`--python 3.13` is the Python the server is tested on, and uv downloads it if you don't have it. Without it uvx takes the newest Python on your machine, and on one too new for a dependency the installation fails: PyYAML, for example, had no package for Python 3.15 yet.

Or with pip, in an environment of your choice:

```bash
pip install mcp-data-platform-toolkit
mcp-data-platform-toolkit --check
```

To run the latest version from GitHub instead of PyPI: `uvx --python 3.13 --from git+https://github.com/akabyte-labs/data-platform-toolkit mcp-data-platform-toolkit --check`.

`--check` prints the server version, the interpreter in use, the dependencies found, whether the Microsoft bridge is available, [your folder](configuration.md#your-folder) and **ready-to-paste client configurations** (in read-only mode). It does not start the MCP protocol.

## Register the server in your MCP client

Register the server in your MCP client. For Claude Desktop, add it to `claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS; see [`examples/mcp_client_config.example.json`](../examples/mcp_client_config.example.json)):

```json
{
  "mcpServers": {
    "data-platform-toolkit": {
      "command": "uvx",
      "args": ["--python", "3.13", "mcp-data-platform-toolkit"],
      "env": { "MCP_ACCESS_MODE": "read_only" }
    }
  }
}
```

For VS Code, the same `command`/`args`/`env` go in `.vscode/mcp.json` under `"servers"`. For Claude Code:

```bash
claude mcp add data-platform-toolkit -e MCP_ACCESS_MODE=read_only -- uvx --python 3.13 mcp-data-platform-toolkit
```

If the client says it can't find `uvx`, give its full path in `command` (`where uvx` on Windows, `which uvx` elsewhere). Installed with pip, use the configuration `--check` prints for your Python environment: `command` is that interpreter and `args` start with `-m mcp_data_platform_toolkit`.

The options (access mode, bridge, output budget, allowed folders) are in [configuration.md](configuration.md#options).
