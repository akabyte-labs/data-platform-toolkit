"""
Centralized error handling for MCP tools.

Problem solved: previously every tool caught its own exceptions and returned
a "[error] ..." string. For the MCP protocol that is a *successful* response,
so `isError` was never set and the model had to infer failure from the text.
Raising a `ToolError` instead makes MCPServer produce a response with
`isError=True` and the clean message.

Side benefit: the try/except boilerplate disappears from every tool.
"""

from __future__ import annotations

import functools
import inspect
import sys
import traceback

try:  # mcp >= 2.x (MCPServer)
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:  # mcp 1.x (FastMCP)
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # fallback if the SDK changes its layout
        class ToolError(Exception):  # type: ignore[no-redef]
            """Application-level error raised by an MCP tool."""


class AmbiguousModelError(Exception):
    """
    The given path contains more than one semantic model.

    This is not a tool failure but an incomplete request: `tool_error_handler`
    turns it into a NORMAL response (no isError) that asks the question, so the
    assistant asks the user which model they meant instead of reporting an
    error.
    """

    def __init__(self, path: str, candidates: list[str]) -> None:
        self.path = path
        self.candidates = list(candidates)
        super().__init__(self.question())

    def question(self) -> str:
        shown = self.candidates[:10]
        lines = "\n".join(f"  - {c}" for c in shown)
        extra = f"\n  - ... and {len(self.candidates) - 10} more" if len(self.candidates) > 10 else ""
        # The advice names the .SemanticModel folder first: it is one of the
        # paths listed above and always identifies a single model. Advising
        # only "the .pbip file" sent the caller back to an equally ambiguous
        # call when the .pbip itself was what had been passed.
        return (
            f"Under '{self.path}' there are {len(self.candidates)} semantic models. "
            f"Which one does the request refer to?\n{lines}{extra}\n"
            f"Ask the user which one they mean and repeat the call with that model's "
            f"'.SemanticModel' folder (one of the paths above) or the .pbip file of its project."
        )


def _format(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return f"Path not found: {message}"
    if isinstance(exc, PermissionError):
        return f"Permission denied: {message}"
    return message


# Failures the tools raise on purpose (invalid input, missing file or
# element): one log line is enough. Anything else is a defect, and its stack
# trace is what a bug report needs.
_EXPECTED_ERRORS = (ValueError, LookupError, OSError, ToolError)


def _log_failure(func, exc: BaseException) -> None:
    """
    Logs a tool failure to stderr, the server's log channel (stdout carries
    the MCP protocol). Only the message used to reach the client, and nothing
    was logged: a bug report had no stack trace to start from.
    """
    name = getattr(func, "__name__", "tool")
    print(f"STATUS: tool '{name}' failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not isinstance(exc, _EXPECTED_ERRORS):
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def tool_error_handler(func):
    """
    Converts any unhandled exception into a `ToolError`, so the MCP client
    receives `isError=True`. Preserves the function's signature and docstring
    (used by MCPServer to generate the tool schema and the description shown to
    the model).
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except AmbiguousModelError as exc:
                return exc.question()
            except ToolError as exc:
                _log_failure(func, exc)
                raise
            except Exception as exc:  # noqa: BLE001 - tool boundary
                _log_failure(func, exc)
                raise ToolError(_format(exc)) from exc

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except AmbiguousModelError as exc:
            return exc.question()
        except ToolError as exc:
            _log_failure(func, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - tool boundary
            _log_failure(func, exc)
            raise ToolError(_format(exc)) from exc

    return wrapper


def register_tools(mcp, tools: dict, descriptions: dict | None = None) -> None:
    """
    Registers a {name: function} dict, applying the error-handling decorator to each.

    `descriptions` overrides the description of individual tools: needed when
    the text shown to the model is composed at runtime (e.g. docstring + shared
    guidelines) and cannot live in the docstring without duplicating it. Tools
    not listed keep their docstring.
    """
    override = descriptions or {}
    for name, func in tools.items():
        mcp.add_tool(tool_error_handler(func), name=name, description=override.get(name))
