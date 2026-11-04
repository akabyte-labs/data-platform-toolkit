# Design decisions

[Back to the README](../README.md)

An AI assistant that can read and edit a Power BI project is useful only if you can trust both what it says and what it writes. Most of the design follows from two questions: what happens when the model is wrong, and what happens when someone tries to make it wrong.

- [Read the files, not a running model](#read-the-files-not-a-running-model)
- [Read-only unless you say otherwise](#read-only-unless-you-say-otherwise)
- [The model never chooses where to write](#the-model-never-chooses-where-to-write)
- [Writes that can't corrupt a project](#writes-that-cant-corrupt-a-project)
- [Say what was not covered](#say-what-was-not-covered)
- [Never guess a lineage](#never-guess-a-lineage)
- [Keep the assistant's context small](#keep-the-assistants-context-small)
- [Your files stay out of the project](#your-files-stay-out-of-the-project)
- [Bridge to Microsoft's server, don't rebuild it](#bridge-to-microsofts-server-dont-rebuild-it)
- [Tests on synthetic projects](#tests-on-synthetic-projects)

## Read the files, not a running model

The server reads the PBIP files on disk: TMDL for the semantic model, PBIR for the reports, the `.sql` files of the database project.

**Why:** the files are what is under version control and what gets reviewed. Reading them needs no Power BI Desktop, no sign-in and no network, works on Linux as well as Windows, and answers questions a running model can't: which reports use a field, and where its data comes from.

## Read-only unless you say otherwise

In read-only mode, the default, the write tools are not registered at all: the assistant can't call what it can't see. The configuration printed by `--check` is read-only too.

**Why:** a refusal inside a write tool still leaves the decision to the model. And a configuration that suggested full access would remove the safe default through a copy-paste, at the very first use.

## The model never chooses where to write

- Write tools work only inside the project they were given, and names are checked so that `../../x` can't leave the model folder. `MCP_ALLOWED_ROOTS` limits which projects can be reached at all.
- Some answers come from the user through a form shown by the client (MCP elicitation): where to keep the user's own files, and whether to save the lineage structure inferred from the SQL. The server receives the answer directly; the parameter that carries it is left out of the tool's input schema, so the model can't fill it.
- A lineage export can only be saved with the extension of its format (`.md`, `.mmd`, `.csv`, `.json`), never as a script.

**Why:** a model reads text it doesn't control (a SQL comment, a measure description, a report title). An instruction hidden there must not be able to choose a folder, approve a save, or write an executable file.

## Writes that can't corrupt a project

- A single function writes every TMDL file. It writes to a temporary file and swaps it in with an atomic rename, so a failure never leaves half a file.
- It keeps the file's line endings and BOM, so an edit doesn't rewrite lines nobody touched.
- It refuses to overwrite a file that changed after it was read (for example, saved meanwhile in Power BI Desktop).
- Values that would break the TMDL syntax are rejected before writing: line breaks in a single-line value, control characters, and the ```` ``` ```` fence, which would hide every object after it.

**Why:** a Power BI project that no longer opens is worse than a wrong answer, and the person reviewing the diff should see only the change that was asked for.

## Say what was not covered

Every response states what it left out:

- the `Coverage` line of the end-to-end lineage;
- reports `NOT analyzed` (PBIR-Legacy);
- matches found only in a text field (a title, a `queryRef`), marked `to verify`;
- references that can't be resolved, listed as unresolved;
- a response that had to be cut starts with `[!] INCOMPLETE RESPONSE`.

The instructions the server gives the assistant at connection ask it to repeat these, and never to say "all the reports" or "not used" without them.

**Why:** "this measure is not used" is the answer that leads someone to delete it. It must not come from a scan that skipped a report.

## Never guess a lineage

- What the SQL states is followed; what it doesn't (dynamic SQL, `INSERT ... EXEC`, a pipeline outside the SQL) is reported with its location, never filled in.
- The loads the SQL can't show are described as rules in `lineage.yaml`, written or confirmed by the user.
- A configuration drafted by the server says `draft: true` until the user removes it, and every lineage response says it is a draft.
- A structure inferred for a project with no configuration is marked `PROVISIONAL` until the user saves it.

**Why:** a wrong layer or a wrong rule doesn't raise an error; it silently skews every answer built on it. So the server makes the uncertainty visible instead of hiding it.

## Keep the assistant's context small

Responses are compact text within a character budget (`MCP_MAX_OUTPUT_CHARS`, 20,000 by default). When a response is too long, it first loses detail and keeps every element; only lists that are already one line per element are cut, and then the response says so at the top.

**Why:** the DAX lineage of a mid-sized model (80 tables, 1,200 measures) runs well over 200,000 characters. Dumped into the conversation it fills the assistant's context; cut silently, it makes the assistant conclude that something doesn't exist.

## Your files stay out of the project

Your description rules and lineage configurations live in a folder of your own, outside the projects. A project's repository gets a file only when you ask for it (`scope='repo'`).

**Why:** a project folder is often a customer's repository, and a personal file must not end up committed there by mistake.

## Bridge to Microsoft's server, don't rebuild it

The edits this server doesn't make on its own (relationships, partitions, hierarchies, RLS filters…) go through Microsoft's official Power BI Modeling MCP Server, behind a bridge:

- it connects only to the local PBIP folder, never to Power BI Desktop or the cloud;
- it exposes an allow-list of the official tools;
- it says which expected tools are missing when the official server changes.

**Why:** Microsoft's server already edits the whole model correctly. Rewriting it would add risk without adding anything; the value of this server is in what the model alone can't answer.

## Tests on synthetic projects

The tests build synthetic PBIP projects, report workspaces and database projects in temporary folders, including a model written the way Power BI Desktop serializes it. CI runs them on Windows and Linux, with the oldest and the newest supported Python, and a separate job installs the built package and runs it outside the repository. The lineage example in the documentation is checked against the real export.

**Why:** no real customer project can be published or shared. Synthetic projects can, and they let anyone reproduce a bug from an anonymized snippet.
