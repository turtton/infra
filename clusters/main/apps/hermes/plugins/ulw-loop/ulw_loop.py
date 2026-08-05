"""ULW-loop command handler — the ``/ulw-loop`` entry point.

Creates a Kanban task and initializes durable ULW-loop state (goals.json,
ledger.jsonl) for the session, enabling crash recovery and multi-agent
orchestration.
"""

import json
import logging
import subprocess
import shlex
import time
from typing import Optional

from . import state as st
from . import phases as ph
from . import tokens as tk

logger = logging.getLogger(__name__)

ORCHESTRATOR_PROFILE = "orchestrator"


def _run_hermes_kanban(args: list[str]) -> dict:
    """Run ``hermes kanban <args>`` and return parsed JSON result."""
    cmd = ["hermes", "kanban"] + args
    logger.debug("Running: %s", shlex.join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
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
        return {"id": stdout.split()[0] if stdout else "unknown"}


def init_ulw_loop(
    goal: str,
    context: str = "",
    platform: str = "",
    chat_id: str = "",
    thread_id: str = "",
) -> dict:
    """Programmatic entry point for mid-conversation ULW-loop activation.

    Creates a Kanban task, initialises durable state with the conversation
    context embedded, and returns the task/session identifiers.

    When ``platform`` and ``chat_id`` are provided, the originating chat is
    auto-subscribed to the task's terminal events (completed, blocked,
    gave_up, crashed, timed_out) — so blocked/status notifications arrive
    back in the Discord/Telegram/etc. thread where ULW-loop was triggered.

    The orchestrator profile picks up the Kanban task via auto-decompose
    and receives the context in its system prompt.

    Args:
        goal: Brief goal description (used as Kanban task title).
        context: Full conversation context / discussion history.  This is
            stored in UlwState.conversation_context and injected into the
            phase prompt so downstream profiles see it.
        platform: Messaging platform (e.g. "discord", "telegram"). If
            provided along with ``chat_id``, auto-subscribes the chat.
        chat_id: Platform chat/channel ID to subscribe.
        thread_id: Optional platform thread/topic ID.

    Returns:
        dict with keys:
          - success: bool
          - task_id: str  (Kanban task id, also used as ULW-loop session id)
          - session_id: str  (same as task_id)
          - message: str  (human-readable status)
    """
    if not goal.strip():
        return {
            "success": False,
            "task_id": "",
            "session_id": "",
            "message": "Goal is required",
        }

    # Phase 1: Init Kanban and create task
    kanban_body = (
        f"# ULW-loop Goal\n\n{goal}\n\n"
        "## Conversation Context\n"
        f"{context}\n\n"
        if context else ""
    ) + (
        "## Workflow\n"
        "1. Orchestratorがこのゴールを子タスクに分解\n"
        "2. 各子タスクが並列/直列で実行 → レビュー → 修正\n"
        "3. 全子タスク完了後、Orchestratorが全体レビュー\n"
        "4. ゴール達成確認 → 完了\n\n"
        "**Note:** The conversation context above contains the discussion "
        "history, requirements, and decisions that led to this goal. "
        "Use it to inform decomposition and implementation.\n\n"
        "## Completion Protocol\n"
        "子タスク作成時に、各タスクのbody末尾に以下の完了手順を必ず含めること:\n"
        "\n"
        "### 完了手順\n"
        "- ユーザーの操作 (API Key設定、承認、マージ等) が不要な場合:\n"
        "  実装完了後、通常通り `<request_review>` で verify → review へ進む\n"
        "- ユーザーの操作が必要な場合:\n"
        "  1. タスクにコメントで以下の情報を残す:\n"
        "     - 完了した作業内容\n"
        "     - ユーザーに必要な操作とその手順\n"
        "     - 完了後に実行するコマンド: `kanban complete <task-id>`\n"
        "  2. blocked (review-required) 状態で終了する\n"
        "  3. ユーザーが操作完了後、上記コマンドでタスクを完了 → 子タスクが自動昇格\n\n"
        "### タスク完了後の流れ\n"
        "- ユーザー操作不要で完了 → verify → review に進む\n"
        "- ユーザー操作必要でblocked → ユーザーが `kanban complete` → 子タスク昇格\n\n"
        f"{tk.token_help_text()}"
    )

    try:
        _run_hermes_kanban(["init"])

        task = _run_hermes_kanban([
            "create", goal,
            "--assignee", ORCHESTRATOR_PROFILE,
            "--body", kanban_body,
            "--priority", "2",
            "--json",
        ])
    except RuntimeError as e:
        logger.error("Failed to create kanban task: %s", e)
        return {
            "success": False,
            "task_id": "",
            "session_id": "",
            "message": f"Kanban task creation failed: {e}",
        }

    task_id = task.get("id", "unknown")

    # Phase 2: Auto-subscribe the originating chat if platform info is available
    subscribed = False
    if platform and chat_id:
        try:
            sub_args = [
                "notify-subscribe", task_id,
                "--platform", platform,
                "--chat-id", chat_id,
            ]
            if thread_id:
                sub_args += ["--thread-id", thread_id]
            _run_hermes_kanban(sub_args)
            subscribed = True
            logger.info(
                "Subscribed %s/%s to task %s",
                platform, chat_id, task_id,
            )
        except RuntimeError as e:
            logger.warning("Failed to subscribe to task %s: %s", task_id, e)

    # Phase 3: Initialize durable ULW-loop state
    session_id = task_id
    state_obj = st.UlwState(
        session_id=session_id,
        brief=goal,
        phase=ph.PHASE_EXPLORE,
        kanban_task_id=task_id,
        created_at=time.time(),
        updated_at=time.time(),
        conversation_context=context,
    )

    # Generate initial goals (heuristic decomposition)
    goals = _decompose_goal(goal)
    state_obj.goals = goals

    # Save state and ledger
    st.save_goals(state_obj)
    st.ledger_append(session_id, "ulw:start", {
        "brief": goal,
        "task_id": task_id,
        "has_context": bool(context),
        "context_length": len(context),
        "goals": [{"id": g.id, "title": g.title, "criteria": len(g.criteria)}
                   for g in goals],
    })
    st.save_resume(session_id, state_obj.phase, state_obj.iteration)

    return {
        "success": True,
        "task_id": task_id,
        "session_id": session_id,
        "subscribed": subscribed,
        "message": (
            f"✅ **ULW-loop起動**\n\n"
            f"**目標:** {goal}\n"
            f"**KanbanタスクID:** `{task_id}`\n"
            f"**コンテキスト:** {'あり (' + str(len(context)) + '文字)' if context else 'なし'}\n"
            f"**担当:** `{ORCHESTRATOR_PROFILE}` (triage → 自動分解)\n"
            + (
                f"**通知:** ✅ このチャットに購読済み（blocked/completed等が自動通知されます）\n"
                if subscribed else
                ""
            ) +
            "\n"
            f"**自動生成ゴール:**\n" +
            "\n".join(
                f"  - `{g.id}`: {g.title} ({len(g.criteria)} criteria)"
                for g in goals
            ) +
            f"\n\n**進行状況の確認:**\n"
            f"- Dash: `hermes dashboard` → Kanban\n"
            f"- CLI: `hermes kanban show {task_id}`\n"
            f"- Watch: `hermes kanban watch`\n"
            f"- Steer: `/ulw-steer {session_id} <action> ...`"
        ),
    }

def handle_ulw_command(raw_args: str) -> Optional[str]:
    """Handle the ``/ulw-loop <goal>`` slash command.

    Delegates to ``init_ulw_loop()`` for the actual work, then formats
    the result as a text message for the user.

    When called from a gateway session (Discord, Telegram, etc.), the
    session registry (updated by ``on_session_start``) is read to
    auto-subscribe the originating chat to the new task's events.
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
            "4. 全タスク完了後、orchestratorが全体ゴールを確認して完了\n\n"
            "**フェーズ:**\n"
            f"  {tk.token_help_text()}"
        )

    # Try to detect current session's platform for auto-subscribe
    session_info = st.load_session_registry()
    platform = session_info.platform if session_info else ""
    chat_id = session_info.chat_id if session_info else ""
    thread_id = session_info.thread_id if session_info else ""

    result = init_ulw_loop(
        goal,
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if result.get("success"):
        return result["message"]
    return (
        f"❌ **ULW-loop起動エラー**\n"
        f"{result['message']}\n\n"
        f"確認事項:\n"
        f"- `hermes kanban init` が実行済みか\n"
        f"- `{ORCHESTRATOR_PROFILE}` プロファイルが存在するか (`hermes profile list`)\n"
        f"- ゲートウェイが起動しているか (`hermes gateway status`)"
    )


def _decompose_goal(goal_text: str) -> list[st.Goal]:
    """Heuristically decompose a goal into sub-goals with acceptance criteria.

    This is a simple heuristic that works for many software development goals.
    The orchestrator profile can refine this via Kanban's auto_decompose.
    """
    goal_lower = goal_text.lower()

    goals = []

    # Detect pattern: "implement X" → research → design → implement → test
    if any(w in goal_lower for w in ["implement", "build", "create", "add", "develop"]):
        goals.append(st.Goal(
            id="g_001", title="要件分析と設計",
            description="機能要件を整理し、アーキテクチャを設計する",
            criteria=[
                st.Criterion("全ての機能要件がリストアップされている"),
                st.Criterion("アーキテクチャ図またはデータフローが定義されている"),
                st.Criterion("使用する技術スタックが決定されている"),
            ],
        ))
        goals.append(st.Goal(
            id="g_002", title="コア実装",
            description="主要な機能を実装する",
            criteria=[
                st.Criterion("Happy path が動作する"),
                st.Criterion("エラーハンドリングが実装されている"),
                st.Criterion("コードが読みやすく、コメントがある"),
            ],
        ))
        goals.append(st.Goal(
            id="g_003", title="テスト",
            description="ユニットテストと結合テストを作成する",
            criteria=[
                st.Criterion("エッジケースのテストが含まれている"),
                st.Criterion("正常系のテストが含まれている"),
                st.Criterion("全てのテストがパスする"),
            ],
        ))
        goals.append(st.Goal(
            id="g_004", title="レビューと修正",
            description="コードレビューを実施し、指摘事項を修正する",
            criteria=[
                st.Criterion("コードレビューが実施されている"),
                st.Criterion("全ての指摘が修正された"),
                st.Criterion("最終確認が完了した"),
            ],
        ))

    elif any(w in goal_lower for w in ["research", "investigate", "survey", "analysis"]):
        goals.append(st.Goal(
            id="g_001", title="情報収集",
            description="関連情報や既存の知見を収集する",
            criteria=[
                st.Criterion("主要な情報源が特定されている"),
                st.Criterion("情報が整理されている"),
            ],
        ))
        goals.append(st.Goal(
            id="g_002", title="分析と考察",
            description="収集した情報を分析し、示唆を得る",
            criteria=[
                st.Criterion("分析結果が文書化されている"),
                st.Criterion("具体的な推奨事項がある"),
            ],
        ))
        goals.append(st.Goal(
            id="g_003", title="レポート作成",
            description="調査結果をレポートにまとめる",
            criteria=[
                st.Criterion("レポートが完了している"),
                st.Criterion("第三者にも理解できる内容である"),
            ],
        ))

    else:
        # Generic decomposition
        goals.append(st.Goal(
            id="g_001", title="目標の明確化",
            description="目標を具体化し、スコープを定義する",
            criteria=[
                st.Criterion("成功基準が定義されている"),
                st.Criterion("スコープが明確である"),
            ],
        ))
        goals.append(st.Goal(
            id="g_002", title="実行",
            description="目標を達成するための作業を実行する",
            criteria=[
                st.Criterion("作業が完了している"),
                st.Criterion("品質基準を満たしている"),
            ],
        ))
        goals.append(st.Goal(
            id="g_003", title="確認と完了",
            description="結果を確認し、完了を宣言する",
            criteria=[
                st.Criterion("成功基準を満たしている"),
                st.Criterion("関係者と合意が取れている"),
            ],
        ))

    return goals


# ---------------------------------------------------------------------------
# CLI command — agent-mediated activation
# ---------------------------------------------------------------------------
# The plugin directory is `ulw-loop` (hyphenated), so `from ulw_loop import
# init_ulw_loop` is NOT importable from a plain subprocess (e.g. execute_code).
# Registering a `hermes ulw-loop` CLI subcommand gives the agent a reliable
# terminal entry point for mid-conversation ULW-loop activation.

def ulw_loop_cli_setup(sp) -> None:
    """Argparse setup for ``hermes ulw-loop <goal>``."""
    sp.add_argument(
        "goal",
        help="Brief goal description (used as Kanban task title)",
    )
    sp.add_argument(
        "--context", default="",
        help="Conversation context / discussion history embedded into the task",
    )
    sp.add_argument(
        "--platform", default="",
        help="Messaging platform (e.g. discord) for auto-subscribe",
    )
    sp.add_argument(
        "--chat-id", default="",
        help="Platform chat/channel ID to subscribe",
    )
    sp.add_argument(
        "--thread-id", default="",
        help="Optional platform thread/topic ID to subscribe",
    )


def ulw_loop_cli_run(args) -> int:
    """Handler for ``hermes ulw-loop`` — delegates to ``init_ulw_loop``."""
    result = init_ulw_loop(
        args.goal,
        context=args.context,
        platform=args.platform,
        chat_id=args.chat_id,
        thread_id=args.thread_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1
