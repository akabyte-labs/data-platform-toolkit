"""
The first-time choice of the user's folder (see `user_folder`), asked in a
form the client shows to the user: MCP elicitation, so the answer reaches
the server without going through the language model.

A tool takes it as a parameter of type `FirstChoice`: MCPServer fills it
before the tool runs and leaves it out of the tool's input schema, so the
model can never set it. The question is asked once, only to clients that
can show it; without an answer the default folder is kept and remembered.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from mcp_data_platform_toolkit.mcp_common import user_folder


class FolderChoice(BaseModel):
    """The form shown to the user, through the client, the first time."""

    location: Literal["settings", "documents", "other"] = Field(
        default="settings",
        title="Folder",
        description="settings = the settings folder (recommended); documents = Documents, "
                    "easier to open; other = the folder written below.")
    folder: str = Field(default="", title="Other folder",
                        description="The whole path of the folder, only for 'other'.")


def choice_message() -> str:
    return (
        "Data Platform Toolkit keeps your own files (your rules for the descriptions, your "
        "lineage configurations) in a folder of yours, outside the projects' repositories. "
        "Where should it keep them?\n"
        f"- settings: {user_folder.default_folder()} (recommended)\n"
        f"- documents: {user_folder.documents_folder()} (easier to open)\n"
        "- other: the folder you write below\n"
        "Without an answer the settings folder is used; you can move them later with "
        "'mcp-data-platform-toolkit --user-folder'."
    )


def elicitation_supported(ctx) -> bool:
    """True when the client can show a form to the user (the SDK's own test)."""
    try:
        capabilities = getattr(ctx, "client_capabilities", None)
    except ValueError:                  # called outside a request (MCPServer.call_tool): no client to ask
        return False
    elicitation = getattr(capabilities, "elicitation", None)
    return elicitation is not None and (getattr(elicitation, "form", None) is not None
                                        or getattr(elicitation, "url", None) is None)


def ask_user_folder(ctx: Context):
    """
    Resolver of `FirstChoice`: the question, only the first time and only to
    a client that can show it. Any other client gets no question, and
    `settle_first_choice` keeps the default.
    """
    if not user_folder.needs_choice() or not elicitation_supported(ctx):
        return None
    return Elicit(choice_message(), FolderChoice)


# A tool parameter of this type is filled by the user's answer, never by the
# language model: MCPServer leaves it out of the tool's input schema.
FirstChoice = Annotated[ElicitationResult[FolderChoice], Resolve(ask_user_folder)]


def settle_first_choice(choice=None) -> str:
    """
    Applies the outcome of the first-time question and returns what to tell
    the user ("" when the folder was already chosen). `choice` is the
    elicitation outcome; without an accepted answer the default is kept and
    remembered, so the question is never asked twice.
    """
    if not user_folder.needs_choice():
        return ""
    data = getattr(choice, "data", None)
    chose = isinstance(data, FolderChoice)
    try:
        if chose and data.location != "settings":
            folder = data.folder if data.location == "other" else user_folder.documents_folder()
            target = user_folder.choose_folder(folder)
        else:
            target = user_folder.choose_folder(user_folder.default_folder())
    except (OSError, ValueError) as exc:
        try:
            target = user_folder.choose_folder(user_folder.default_folder())
        except OSError as inner:
            return f"Note: your folder could not be created ({inner}); nothing of yours is used."
        return f"The chosen folder can't be used ({exc}): your files are kept in '{target}'."
    if chose:
        return f"Your files are kept in '{target}', as you chose."
    return (
        f"First use: your files (rules for the descriptions, lineage configurations) are kept "
        f"in '{target}', the default folder. To keep them elsewhere, run "
        f"'mcp-data-platform-toolkit --user-folder' in a terminal, or ask to move them to the "
        f"Documents folder."
    )
