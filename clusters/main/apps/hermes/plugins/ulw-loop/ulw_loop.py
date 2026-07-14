"""ULW-loop handler — the `/ulw-loop` command implementation.

Workflow:
  1. User invokes `/ulw-loop <goal>` from Discord / CLI / any gateway
  2. Creates a Kanban task in `triage` column, assigned to `orchestrator`
  3. Returns the task ID and next-steps guidance to the user
  4. (Background) Kanban's auto_decompose picks up the triage task and
     the orchestrator profile decomposes it into a task graph
  5. Each sub-task runs through its own execute→review→fix loop
  6. When all children complete, the orchestrator re-awakens to verify
     the global goal and close the parent task
"""

import json
import logging
import subprocess
import shlex

logger = logging.getLogger(__name__)

# Default profile names — user can override these in kanban config
ORCHESTRATOR_PROFILE = "orchestrator"


def _run_hermes_kanban(args: list[str]) -> dict:
    """Run ``hermes kanban <args>`` and return parsed JSON result.

    Returns a dict with at least ``{"id": "..."}`` on success, or raises
    on fatal errors.
    """
    cmd = ["hermes", "kanban"] + args
    logger.debug("Running: %s", shlex.join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("hermes kanban command timed out after 30s")
    except FileNotFoundError:
        raise RuntimeError(
            "`hermes` command not found — is Hermes Agent installed and on PATH?"
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"kanban command failed (exit {result.returncode}): {stderr or result.stdout[:200]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        return {"id": "unknown"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        # Fallback: extract task ID from human-readable output
        return {"id": stdout.split()[0] if stdout else "unknown"}


def handle_ulw_command(raw_args: str) -> str | None:
    """Handle the ``/ulw-loop <goal>`` slash command.

    Args:
        raw_args: Everything after ``/ulw-loop`` in the user's message.
                  May be empty — returns usage message in that case.

    Returns:
        A user-facing response message, or ``None`` if the command should
        produce no reply (unused here, but supported by the plugin API).
    """
    goal = raw_args.strip()
    if not goal:
        return (
            "**使用方法:** `/ulw-loop <目標>`\n\n"
            "**例:**\n"
            "  `/ulw-loop ユーザー認証モジュールを実装する`\n"
            "  `/ulw-loop 週次インシデントレポートを自動生成する`\n\n"
            "**どう動くか:**\n"
            "1. 目標をKanbanの`triage`に登録\n"
            "2. `orchestrator`プロファイルが自動分解 → 子タスク生成\n"
            "3. 各子タスクを担当プロファイルが実行・レビュー・修正\n"
            "4. 全タスク完了後、orchestratorが全体ゴールを確認して完了"
        )

    # --- Phase 1: Create the parent task in Kanban triage ---
    try:
        # Kanban init is idempotent — safe to call every time
        _run_hermes_kanban(["init"])

        task = _run_hermes_kanban([
            "create", goal,
            "--assignee", ORCHESTRATOR_PROFILE,
            "--body", (
                f"# ULW-loop Goal\n\n{goal}\n\n"
                "## Acceptance Criteria\n"
                "(Orchestrator: 分解時に各子タスクに acceptance criteria を設定してください)\n\n"
                "## Workflow\n"
                "1. Orchestratorがこのゴールを子タスクに分解\n"
                "2. 各子タスクが並列/直列で実行 → レビュー → 修正\n"
                "3. 全子タスク完了後、Orchestratorが全体レビュー\n"
                "4. ゴール達成確認 → 完了"
            ),
            "--priority", "2",
            "--json",
        ])
    except RuntimeError as e:
        logger.error("Failed to create kanban task: %s", e)
        return (
            f"❌ **ULW-loop起動エラー**\n"
            f"Kanbanタスク作成に失敗しました:\n"
            f"`{e}`\n\n"
            f"確認事項:\n"
            f"- `hermes kanban init` が実行済みか\n"
            f"- `{ORCHESTRATOR_PROFILE}` プロファイルが存在するか (`hermes profile list`)\n"
            f"- ゲートウェイが起動しているか (`hermes gateway status`)"
        )

    task_id = task.get("id", "unknown")

    # --- Return success with guidance ---
    return (
        f"✅ **ULW-loop起動**\n\n"
        f"**目標:** {goal}\n"
        f"**KanbanタスクID:** `{task_id}`\n"
        f"**担当:** `{ORCHESTRATOR_PROFILE}` (triage → 自動分解)\n\n"
        f"**進行状況の確認:**\n"
        f"- ダッシュボード: `hermes dashboard` → Kanbanタブ\n"
        f"- CLI: `hermes kanban show {task_id}`\n"
        f"- ウォッチ: `hermes kanban watch`\n\n"
        f"**次のステップ:**\n"
        f"1. Orchestratorが`triage`のタスクを検出 → 自動分解\n"
        f"2. 各サブタスクが`ready` → dispatcherがワーカーを起動\n"
        f"3. タスク完了 → Discordに通知が届きます 🎉"
    )
