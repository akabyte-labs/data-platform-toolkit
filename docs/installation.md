# Installation

[Back to the README](../README.md)

- [Requirements](#requirements)
- [Install and check](#install-and-check)
- [Register the server in your MCP client](#register-the-server-in-your-mcp-client)
- [Enable the write tools](#enable-the-write-tools)
- [Update](#update)
- [Troubleshooting](#troubleshooting)

## Requirements

- **Python 3.10 to 3.13**, on Windows, Linux or macOS. CI tests 3.10 and 3.13, on Windows and Linux. A newer Python works once every dependency publishes a package for it. The server is pure Python: only the optional Microsoft bridge needs Windows.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) is the easiest way to install and run it; `pip` works too.
- A Power BI project saved as **PBIP** with the **TMDL** semantic model format and, for report analysis, the **PBIR** report format. See [From a .pbix to a PBIP project](#from-a-pbix-to-a-pbip-project).
- For the lineage of the data warehouse: a **SQL Server database project** (`.sqlproj`, SSDT or SDK-style) with its `.sql` files.

### From a .pbix to a PBIP project

The server reads project files, not `.pbix` files. In Power BI Desktop, open the `.pbix` and use **File > Save as**, type **Power BI project (\*.pbip)** ([Microsoft's guide](https://learn.microsoft.com/power-bi/developer/projects/projects-overview#save-as-a-project)). Then check two things:

- **The semantic model is in TMDL**: the `.SemanticModel` folder holds a `definition` folder, not a single `model.bim` file. A project saved as TMSL (`model.bim`) can't be read: save it again with the TMDL format ([TMDL format](https://learn.microsoft.com/power-bi/developer/projects/projects-dataset#tmdl-format)).
- **The report is in PBIR**: the `.Report` folder holds a `definition` folder, not a `report.json`. PBIR is the default format of recent Power BI Desktop releases, and saving a PBIR-Legacy report there converts it ([PBIR format](https://learn.microsoft.com/power-bi/developer/projects/projects-report#pbir-format)).

`--check` with the path of the `.pbip` tells you whether the project can be read.

## Install and check

With [uv](https://docs.astral.sh/uv/) there is nothing to install by hand: `uvx` downloads the server the first time, keeps it in its cache and runs it. Check that it works:

```bash
uvx --python 3.13 mcp-data-platform-toolkit --check
uvx --python 3.13 mcp-data-platform-toolkit --check "C:\path\to\Project.pbip"   # also validates a project
```

`--python 3.13` is the newest Python that CI tests, and uv downloads it if you don't have it. Without it, uvx takes the newest Python on your machine, and on one too new for a dependency the installation fails (when Python 3.15 came out, PyYAML had no package for it yet).

Or with pip, in an environment of your choice:

```bash
pip install mcp-data-platform-toolkit
mcp-data-platform-toolkit --check
```

To run the latest version from GitHub instead of PyPI: `uvx --python 3.13 --from git+https://github.com/akabyte-labs/data-platform-toolkit mcp-data-platform-toolkit --check`.

`--check` prints:

- the server version, the Python interpreter and the versions of the dependencies;
- the folders the server may access ([`MCP_ALLOWED_ROOTS`](configuration.md#allowed-folders)) and [your folder](configuration.md#your-folder);
- whether the Microsoft bridge is available;
- with a project path, whether its semantic model and tables are found;
- two **ready-to-paste client configurations**, one with uvx and one with the Python environment it runs in, both read-only (`--access=read_only` in `args`).

It does not start the MCP protocol, so it is safe to run from a terminal at any time.

## Register the server in your MCP client

For **Claude Desktop**, add the server to `claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS; see [`examples/mcp_client_config.example.json`](../examples/mcp_client_config.example.json)), then restart Claude Desktop:

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

For **VS Code**, in `.vscode/mcp.json`:

```json
{
  "servers": {
    "data-platform-toolkit": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--python", "3.13", "mcp-data-platform-toolkit"],
      "env": { "MCP_ACCESS_MODE": "read_only" }
    }
  }
}
```

For **Claude Code**:

```bash
claude mcp add data-platform-toolkit -e MCP_ACCESS_MODE=read_only -- uvx --python 3.13 mcp-data-platform-toolkit
```

If the client says it can't find `uvx`, give its full path in `command` (`where uvx` on Windows, `which uvx` elsewhere). Installed with pip, use the configuration `--check` prints for your Python environment: `command` is that interpreter and `args` start with `-m mcp_data_platform_toolkit`.

## Enable the write tools

The server starts in read-only mode: the write tools (the TMDL edits, `save_description_guidelines`, `save_lineage_config`, `save_lineage_export`, `set_user_folder`, and the bridge's `call_official_powerbi_tool`) are not even registered, and the project is never changed. To enable them, set `MCP_ACCESS_MODE` to `full_access` in `env`, or, in a configuration copied from `--check`, replace `--access=read_only` with `--access=full_access` in `args`. The flag takes precedence over the variable, so with `--access=read_only` in `args` changing only `env` has no effect.

Keep the project under version control (or keep a backup) before enabling them, and consider [`MCP_ALLOWED_ROOTS`](configuration.md#allowed-folders) to confine the server to your projects. All the options are in [configuration.md](configuration.md#options).

## Update

uvx keeps the version it downloaded the first time and does not look for new ones on its own. To move to the latest release, run once:

```bash
uvx --python 3.13 mcp-data-platform-toolkit@latest --check
```

It refreshes uv's cache, and the client runs the new version from its next start. `--check` shows the version in use. With pip: `pip install --upgrade mcp-data-platform-toolkit`.

## Troubleshooting

Start from `--check` in a terminal, with the path of the `.pbip`: it shows what is missing without involving the client.

**Installation and startup**

| What you see | What to do |
|---|---|
| The client can't find `uvx`, or says the server doesn't start | Put the full path of `uvx` in `command` (`where uvx` on Windows, `which uvx` elsewhere), then run `--check` from a terminal. |
| `ERROR: Python 3.10 or later is required` | `command` points to an older Python. Use uvx with `--python 3.13`, or the interpreter the server is installed in. |
| `ERROR: missing or incompatible dependencies` | Reinstall with the interpreter in `command`: the message gives the exact command. |
| The installation fails while building a dependency | Your Python is newer than the dependencies support. Keep `--python 3.13` in the uvx command. |
| The write tools are missing | The server is in read-only mode: see [Enable the write tools](#enable-the-write-tools). |

**Project paths**

| What you see | What to do |
|---|---|
| `No '.SemanticModel' folder found` or `TMDL tables folder does not exist` | The folder is not a PBIP project, or its model is saved as TMSL: see [From a .pbix to a PBIP project](#from-a-pbix-to-a-pbip-project). |
| `... has no local semantic model` | The report is connected to a published model. Pass the path of the `.SemanticModel` folder of a local model. |
| The tool asks which semantic model you mean | The folder holds several projects. Give the path of the `.pbip`. |
| A drive or OneDrive root finds nothing | The search stops 6 folder levels below the given path. Point to the project. |
| `... is outside the folders this server may access` | The path is not in `MCP_ALLOWED_ROOTS`: add its folder ([Allowed folders](configuration.md#allowed-folders)). |

**Reports and Power BI Desktop**

| What you see | What to do |
|---|---|
| Reports listed as `NOT analyzed, upgrade to PBIR` | They are in the PBIR-Legacy format. Open and save them in a recent Power BI Desktop, which converts them to PBIR. |
| Changes written by the server don't show in Power BI Desktop | Close and reopen the project, or see [Known limitations](limitations.md#model-and-reports) for the preview feature that reloads them. |
| `--check` says `[absent] official server not found` | Only the optional bridge is affected. Install the VS Code extension `analysis-services.powerbi-modeling-mcp` or set `POWERBI_MODELING_MCP_PATH` (Windows only). |
