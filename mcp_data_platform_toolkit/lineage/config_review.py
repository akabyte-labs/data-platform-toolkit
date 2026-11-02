"""
The form that shows the warning of a generated lineage configuration to the
user as it is: MCP elicitation, so the words reach the user without going
through the language model (`draft_notice`: a generated file says `draft: true`).

`save_lineage_config` takes the answer as a parameter of type
`LineageReview`: MCPServer fills it before the tool runs and leaves it out of
the tool's input schema, so the model can never set it. The form is a notice
with one choice (open the file in the editor): the file is saved whatever the
answer, so a client that refuses forms by itself can never block a save; the
tool's answer then carries the warning for the assistant to pass on.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, Resolve
from pydantic import BaseModel, Field

from mcp_data_platform_toolkit.lineage.draft_notice import (DRAFT_MARKER, DRAFT_WARNING, HOW_TO_CHECK, is_generated,
                                                            unfenced)
from mcp_data_platform_toolkit.mcp_common.folder_choice import elicitation_supported

__all__ = ["DRAFT_MARKER", "DRAFT_WARNING", "LineageReview", "ReviewAnswer", "ask_review", "is_generated",
           "review_message", "unfenced"]


class ReviewAnswer(BaseModel):
    """The form shown to the user, through the client, when a generated configuration is saved."""

    open_in_editor: bool = Field(
        default=True, title="Open it in my editor",
        description="Open the saved file in an editor, on the computer where the server runs.")


def review_message() -> str:
    return (f"{DRAFT_WARNING} {HOW_TO_CHECK}\n\n"
            f"The file is being saved. Accept to open it in your editor and check it now.")


def ask_review(ctx: Context, content: str = ""):
    """
    Resolver of `LineageReview`: the form, only for a generated file that can
    be saved and only to a client that can show it. Any other save gets no
    question.
    """
    from mcp_data_platform_toolkit.sql_project.config import ConfigError, parse_config
    text = unfenced(content)
    if not is_generated(text) or not elicitation_supported(ctx):
        return None
    try:
        parse_config(text, "lineage.yaml")
    except ConfigError:
        return None                     # the save fails with the problems of the file
    return Elicit(review_message(), ReviewAnswer)


# A tool parameter of this type is filled by the user's answer, never by the
# language model: MCPServer leaves it out of the tool's input schema.
LineageReview = Annotated[ElicitationResult[ReviewAnswer], Resolve(ask_review)]
