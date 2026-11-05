"""hermes-phone: approved outbound phone calls for Hermes Agent (P0/P1: approval and safety)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

__version__ = "0.1.0"

SKILL_NAME = "phone-calls"
SKILL_PATH = Path(__file__).resolve().parent.parent / "skills" / SKILL_NAME / "SKILL.md"


def register(ctx: Any) -> None:
    """Register tools, hooks, the /phone command and the bundled skill.

    Must not open sockets or import heavy dependencies: Plugin Doctor blocks the network while
    this runs. The SQLite store is opened lazily on first use.
    """
    from . import cli, commands, hooks, tools

    tools.configure(ctx=ctx)
    for name, handler in tools.HANDLERS.items():
        ctx.register_tool(name=name, toolset="phone", schema=tools.SCHEMAS[name],
                          handler=handler, description=tools.SCHEMAS[name]["description"])
    ctx.register_hook("pre_tool_call", hooks.pre_tool_call)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_command("phone", commands.phone_command,
                         description="Approve, cancel or inspect phone calls",
                         args_hint="approve <id> | cancel <id> | status [id] | transcript <id> "
                                   "| log [n] | retry <id> | consent <id> <field> "
                                   "| answer <qid> [text] | hangup [id]")
    ctx.register_cli_command(
        name="phone", help="Run the phone media bridge and check the setup",
        setup_fn=cli.setup, handler_fn=cli.make_handler(ctx),
        description="hermes phone serve | doctor")
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is not None and SKILL_PATH.exists():
        register_skill(SKILL_NAME, SKILL_PATH,
                       "How and when to place phone calls for the user, safely.")
