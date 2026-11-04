# Security Policy

## Supported versions

Only the latest release (on [PyPI](https://pypi.org/project/mcp-data-platform-toolkit/), tagged on the `main` branch) receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: open the repository's **Security** tab and select **Report a vulnerability**. Include a description of the issue, steps to reproduce it and its potential impact.

This is a volunteer-maintained project: reports are reviewed on a best-effort basis, and you will be kept informed of progress.

## Security model

Why the server is built this way: [docs/design.md](docs/design.md).

### What the server can touch

- Apart from the user's own folder ([where it is](docs/configuration.md#your-folder)), the server reads and writes files only under the project paths passed to its tools. Table and role names are validated as simple names, so a name such as `../../x` cannot escape the model folder.
- The project path itself is chosen by the assistant. Set `MCP_ALLOWED_ROOTS` to the folders it may use: any project, model or report outside them is refused, also when reached through a `.pbip` file or a report bound to a model elsewhere. Recommended in `full_access` mode.
- A lineage export (`save_lineage_export`) is saved only inside the folder of the Power BI project or of the database project it describes (inside `MCP_ALLOWED_ROOTS` when set), and only with the extension of its format (`.md`, `.mmd`, `.csv`, `.json`): never a script or an executable.
- The optional bridge to the official Microsoft server connects only to local PBIP folders. It exposes an allow-list of official tools and blocks the operations that connect to Power BI Desktop, Fabric or other cloud services. Path arguments passed to official tools must be absolute and inside the project's semantic model.

### Read-only by default

- Write tools are registered only with `MCP_ACCESS_MODE=full_access` (or `--access=full_access`).
- In both modes the server never changes a project by itself. Without full access it writes only in the user's own folder:
  - the choice of that folder;
  - the template of a project's rules file;
  - the user's answer to the first-time question about description rules;
  - the structure of a database project proposed by the lineage tools, only after the user chose **save** in a form.

### What the language model can't decide

Some answers come from a form the client shows to the user (MCP elicitation). They reach the server without going through the language model, so a prompt injected in a SQL comment or a model description can't give them.

- **Where the user's folder is**: chosen by the user in a form the first time, or the default is kept. Later a tool can only switch between the settings folder and Documents; any other folder is typed in a terminal (`--user-folder`) or set by `MCP_USER_FOLDER`.
- **Saving an inferred lineage structure**: only when the user answers **save** in a form. A client that can't show forms gets the proposal through the assistant, and nothing is written in read-only mode. The repository's own `lineage.yaml` is never replaced.
- **Writing in a repository**: only when asked (`scope='repo'`, full access), and only inside the project's folders (and `MCP_ALLOWED_ROOTS`, when set).

### Safe writes

- Names and property values containing line breaks or control characters are rejected before being written, so they cannot inject extra TMDL properties. DAX expressions and descriptions may span several lines, but any other control character is rejected, and so is ```` ``` ```` (the TMDL fence, which would hide every object after it).
- Writes are atomic and refuse to overwrite a file that changed after it was read.

Always review changes made by an AI assistant before committing them.
