# Security Policy

## Supported versions

Only the latest version on the `main` branch receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Use GitHub's private reporting instead: open the repository's **Security** tab and select **Report a vulnerability**. Include a description of the issue, steps to reproduce it and its potential impact.

This is a volunteer-maintained project: reports are reviewed on a best-effort basis, and you will be kept informed of progress.

## Security model

- The server reads and writes files only under the project paths passed to its tools. Table and role names are validated as simple names, so a name such as `../../x` cannot escape the model folder.
- The project path itself is chosen by the assistant. Set `MCP_ALLOWED_ROOTS` to the folders it may use: any project, model or report outside them is refused, also when reached through a `.pbip` file or a report bound to a model elsewhere. Recommended in `full_access` mode.
- Names and property values containing line breaks or control characters are rejected before being written, so they cannot inject extra TMDL properties. DAX expressions and descriptions may span several lines, but any other control character is rejected, and so is ```` ``` ```` (the TMDL fence, which would hide every object after it).
- Writes are atomic and refuse to overwrite a file that changed after it was read.
- A lineage export (`save_lineage_export`) is saved only inside the folder of the Power BI project or of the database project it describes (inside `MCP_ALLOWED_ROOTS` when set), and only with the extension of its format (`.md`, `.mmd`, `.csv`, `.json`): never a script or an executable, never elsewhere on the disk.
- It runs in **read-only** mode by default; write tools require `MCP_ACCESS_MODE=full_access`. In both modes the server never changes a project by itself; without full access it only writes in the user's own folder (see below): the first-time choice of that folder, and the structure of a database project that the lineage tools propose, saved only after the user chose **save** in a form shown by the client.
- **The user's own files** (rules for the descriptions, lineage configurations) live outside the projects: in the settings folder (`%APPDATA%\data-platform-toolkit`, `~/.config/data-platform-toolkit`), in Documents, or in a folder the user chose. The language model never picks that folder: the first time the user answers a form shown by the client (MCP elicitation; the answer doesn't go through the model), or the default is kept; later a tool can only switch between the settings folder and Documents, and any other folder is typed in a terminal (`--user-folder`) or set by `MCP_USER_FOLDER`. A repository gets a file only when asked (`scope='repo'`, full access), and only inside the project's folders (and `MCP_ALLOWED_ROOTS`, when set). The structure inferred for a database project with no lineage configuration is saved as the user's own file, in their folder, outside the repository, only when the user answers **save** in a form: the answer doesn't go through the language model, so it can't be given by a prompt injected in a SQL comment or a description. A client that can't show forms gets the proposal through the assistant and nothing is written by it in read-only mode; the repository's own `lineage.yaml` is never replaced.
- The optional bridge to the official Microsoft server only connects to local PBIP folders: it exposes an allow-list of official tools, and operations that connect to Power BI Desktop, Fabric or other cloud services are blocked. Path arguments passed to official tools must be absolute and inside the project's semantic model.
- Always review changes made by an AI assistant before committing them.
