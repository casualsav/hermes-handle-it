"""Hermes holds a company's customer-support chat for you.

Hermes opens the chat in its own browser, talks to the support agent for as long as it takes,
and comes back to you only for decisions: agreeing to an offer, paying, cancelling, or sharing a
detail you did not list as shareable. It ends with the outcome and a saved transcript.
"""

from __future__ import annotations

from . import handlers


def register(ctx) -> None:
    handlers.bind(ctx)
    for schema, handler, emoji in handlers.TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset="support_chat",
            schema=schema,
            handler=handler,
            description=schema["description"],
            emoji=emoji,
        )
    ctx.register_hook("pre_tool_call", handlers.pre_tool_call)
    ctx.register_hook("on_session_finalize", handlers.on_session_finalize)
