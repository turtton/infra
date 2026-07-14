"""ULW-loop plugin for Hermes Agent.

Registers the `/ulw-loop` slash command that creates a Kanban task with
orchestrator profile assignment, enabling the full explore→plan→execute→
verify→review→fix workflow across multiple Hermes profiles.

The command is auto-registered as a Discord native slash command by the
Discord adapter, and works on all other gateway platforms automatically.
"""

from . import ulw_loop


def register(ctx):
    """Register the `/ulw-loop` slash command."""
    ctx.register_command(
        name="ulw-loop",
        handler=ulw_loop.handle_ulw_command,
        description="ULW-loop: 目標をKanbanタスクに分解してマルチエージェントで実行する",
        args_hint="<goal description>",
    )
