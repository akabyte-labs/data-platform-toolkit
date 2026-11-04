# Configuration

[Back to the README](../README.md)

- [Options](#options)
- [Your folder](#your-folder)
- [Description rules](#description-rules)
- [Lineage configuration](#lineage-configuration)

## Options

| Environment variable | CLI flag | Values | Default | Description |
|---|---|---|---|---|
| `MCP_ACCESS_MODE` | `--access=` | `read_only`, `full_access` | `read_only` | Write tools are registered only in `full_access` mode. `--access=` takes precedence over the variable. |
| `MCP_MSFT_BRIDGE` | `--msft-bridge=` | `auto`, `on`, `off` | `auto` | `auto` enables the [bridge](tools.md#the-microsoft-bridge) only if the official executable is found (otherwise only `list_official_powerbi_tools` is registered, to explain how to install it); `on` registers it anyway; `off` registers none of its tools. `--msft-bridge=` takes precedence over the variable. |
| `MCP_MAX_OUTPUT_CHARS` | | characters | `20000` | Character budget for the tools' text responses. |
| `MCP_MAX_CACHED_CHARS` | | characters | `4000000` | Size cap of the cache that keeps full bridge results for `get_official_result_detail`. |
| `MCP_ALLOWED_ROOTS` | | folders | | Folders the server may read or write, separated by `;` on Windows and `:` elsewhere. See [Allowed folders](#allowed-folders). |
| `MCP_USER_FOLDER` | | folder | | Fixes [your folder](#your-folder) for every user of the machine. It overrides the user's choice and can't be changed with `--user-folder`. |
| `POWERBI_MODELING_MCP_PATH` | | path | | Full path to `powerbi-modeling-mcp.exe`, if it is not on `PATH` or in the VS Code extensions folder. |
| `POWERBI_MODELING_MCP_TIMEOUT` | | seconds | `120` | Timeout for each call to the official server. |
| `POWERBI_MODELING_MCP_MAX_CHARS` | | characters | `4000` | Character budget for bridge responses before they are reduced. |

> **Tip:** start in `read_only` mode and switch to `full_access` only when you want the assistant to edit files ([how](installation.md#enable-the-write-tools)). Keep your project under version control so you can review every change.

### Allowed folders

When `MCP_ALLOWED_ROOTS` is set:

- a project, model or report outside those folders is refused, even when it is reached indirectly (a `.pbip`, or a report bound to a model elsewhere);
- a lineage export can be saved anywhere inside them.

When it is not set, any project can be read, and a lineage export can be saved only in the folder of the project it describes.

Your folder is always allowed.

## Your folder

Your own files (your description rules and your lineage configurations) live in a folder outside the projects' repositories. A project folder is often a customer's repository, and a personal file must never end up there by mistake. Updating the server never touches this folder.

By default it is `%APPDATA%\data-platform-toolkit` on Windows and `~/.config/data-platform-toolkit` on macOS and Linux (`$XDG_CONFIG_HOME/data-platform-toolkit` when that variable is set):

```
%APPDATA%\data-platform-toolkit\
  settings.json                       where your folder is, and whether you were asked for your rules
                                      (always kept here, in the default folder)
  description_guidelines.md           your description rules for every project
  projects\
    contoso-bi\                       one project, named after its repository
      description_guidelines.md       your description rules for this project only
      Contoso.DWH.lineage.yaml        the lineage configuration of a database project
    fabrikam-bi\
      description_guidelines.md
      Fabrikam.Staging.lineage.yaml
      Fabrikam.Mart.lineage.yaml
```

The server creates a project's subfolder, with its `description_guidelines.md`, the first time it works on that project, so you always know where its files go. Every file is read again at every call: you never need to restart the server after editing or moving them.

### Choosing and moving it

**The first time** a tool needs your folder, the client asks where to keep it: the settings folder (the default), Documents (easier to find) or a folder you type. A client that can't show the question keeps the default, and the server says so in its response.

**To move it later**, run in a terminal (Command Prompt or PowerShell on Windows; with pip, drop `uvx --python 3.13` from every command in this page):

```bash
uvx --python 3.13 mcp-data-platform-toolkit --user-folder "D:\My files"
```

Everything is moved to the new folder, files already there are kept, and the server remembers the new location. Without a folder, the command shows where your folder is now and lets you pick a new one from a menu:

```bash
uvx --python 3.13 mcp-data-platform-toolkit --user-folder
```

From the chat, the assistant can only switch between the settings folder and Documents (`set_user_folder`).

**For a whole office**, `MCP_USER_FOLDER` fixes the folder for everyone, and these commands can't move it.

Your rules and your lineage configurations always move together with the folder: they can't be moved one by one, because the server looks for them only there.

### Sharing with the team

The server writes in a project's repository only when you ask it to (`scope='repo'`). A file the team committed there is read together with yours, in two different ways:

- the team's **description rules** add to yours ([order](#where-they-come-from));
- the team's **lineage configuration** replaces yours ([order](#which-file-is-used)).

## Description rules

`write_smart_description` writes descriptions for a business Data Dictionary: one to three sentences in plain language, with no DAX syntax, no `Table[Column]` notation and no modeling jargon. These are the **built-in rules**, which ship with the server and improve with it. A short version is part of the documentation tools' descriptions; `get_description_guidelines` returns the full version, with examples.

**Your own rules** extend them: terminology, language, level of detail, and a **glossary** of your company's terms. The assistant explains an acronym or an internal term in a description only if the glossary or you define it; otherwise it asks.

### Where they come from

The rules are combined from the most general to the most specific, and where they disagree the more specific one wins:

1. the built-in rules;
2. your rules for every project: `description_guidelines.md` at the top of [your folder](#your-folder);
3. the team's rules, if someone committed them: a `description_guidelines.md` in the project folder or above it, up to the root of its Git repository. The server never creates this file on its own;
4. your rules for one project: `projects\<repository>\description_guidelines.md` in your folder.

A file whose first line is `<!-- replace -->` replaces the rules before it instead of adding to them. Text between `<!--` and `-->` is ignored.

### Writing them

- **The first time** a description is prepared or written, `get_smart_description_preview` or `write_smart_description` (whichever comes first) asks whether you have rules to add. The question appears as a form in the client, or through the assistant when the client can't show forms. Your answer is saved in your rules for every project; an empty answer writes the description straight away. You are asked only once, and never if you already have rules.
- **In an editor**: this command opens your rules for every project in the system's editor, creating the file from a template the first time:

  ```bash
  uvx --python 3.13 mcp-data-platform-toolkit --guidelines
  ```

  Add the path of a `.pbip` to open your rules for that project only. The path only tells the server which project you mean; the file always stays in your folder, never in the project:

  ```bash
  uvx --python 3.13 mcp-data-platform-toolkit --guidelines "C:\path\to\Project.pbip"
  ```

- **From the chat**: say "in this project, call the customers 'clients'", and `save_description_guidelines` (full access) adds the rule to your rules for the project. To share a rule with the team, ask the assistant to save it in the repository (`scope='repo'`).

## Lineage configuration

The lineage configuration is a YAML file that tells the lineage how your database is organized: the layer of each schema (bronze, silver, gold or your own), the schemas to ignore, the rules for the tables nothing loads, and the columns the ETL creates. There is one per database project.

To open it in the system's editor:

```bash
uvx --python 3.13 mcp-data-platform-toolkit --lineage-config "C:\path\to\Project.sqlproj"
```

It opens `projects\<repository>\<database project>.lineage.yaml` in your folder. The server needs the `.sqlproj` to name the file and, when there is no configuration yet, to draft one from the project.

A draft comes with a warning: check it before relying on it. A wrong layer or a schema ignored by mistake makes the whole lineage wrong without any error. Review it yourself or ask the assistant to complete it.

### Which file is used

The server looks for the configuration of a database project in this order, and uses the first one it finds:

1. `config_path`, when a tool is given one;
2. the repository's `lineage.yaml` (or `lineage.yml`) next to the `.sqlproj`, if the team committed one;
3. yours: `projects\<repository>\<database project>.lineage.yaml` in [your folder](#your-folder).

Every lineage response says which file it used, and says so when the repository's file hides yours; `--lineage-config` then opens the repository's file and tells you. Without any file, the standard medallion applies: schemas `bronze`, `silver` and `gold`.

How to write the configuration: [lineage.md](lineage.md#database-lineage-configuration).
