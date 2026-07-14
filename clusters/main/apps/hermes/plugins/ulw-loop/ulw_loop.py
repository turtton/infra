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


def handle_ulw_command(raw_args: str) -> Optional[str]:
    """Handle the ``/ulw-loop <goal>`` slash command.

    Workflow:
      1. Parse the goal description
      2. Create a Kanban task in triage, assigned to orchestrator
      3. Initialize ULW-loop state (goals.json + ledger.jsonl)
      4. Generate initial goals with acceptance criteria
      5. Return task ID and guidance

    The orchestrator profile picks up the task via Kanban's auto-decompose,
    and the lifecycle hooks (pre_llm_call, post_llm_call, pre_verify)
    manage the phase transitions.
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

    # Phase 1: Init Kanban and create task
    try:
        _run_hermes_kanban(["init"])

        task = _run_hermes_kanban([
            "create", goal,
            "--assignee", ORCHESTRATOR_PROFILE,
            "--body", (
                f"# ULW-loop Goal\n\n{goal}\n\n"
                "## Workflow\n"
                "1. Orchestratorがこのゴールを子タスクに分解\n"
                "2. 各子タスクが並列/直列で実行 → レビュー → 修正\n"
                "3. 全子タスク完了後、Orchestratorが全体レビュー\n"
                "4. ゴール達成確認 → 完了\n\n"
                f"{tk.token_help_text()}"
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

    # Phase 2: Initialize durable ULW-loop state
    session_id = task_id  # Use kanban task id as session id
    state_obj = st.UlwState(
        session_id=session_id,
        brief=goal,
        phase=ph.PHASE_EXPLORE,
        kanban_task_id=task_id,
        created_at=time.time(),
        updated_at=time.time(),
    )

    # Generate initial goals (heuristic decomposition)
    goals = _decompose_goal(goal)
    state_obj.goals = goals

    # Save state and ledger
    st.save_goals(state_obj)
    st.ledger_append(session_id, "ulw:start", {
        "brief": goal,
        "task_id": task_id,
        "goals": [{"id": g.id, "title": g.title, "criteria": len(g.criteria)}
                   for g in goals],
    })
    st.save_resume(session_id, state_obj.phase, state_obj.iteration)

    goals_summary = "\n".join(
        f"  - `{g.id}`: {g.title} ({len(g.criteria)} criteria)"
        for g in goals
    )

    return (
        f"✅ **ULW-loop起動**\n\n"
        f"**目標:** {goal}\n"
        f"**KanbanタスクID:** `{task_id}`\n"
        f"**担当:** `{ORCHESTRATOR_PROFILE}` (triage → 自動分解)\n\n"
        f"**自動生成ゴール:**\n{goals_summary}\n\n"
        f"**進行状況の確認:**\n"
        f"- ダッシュボード: `hermes dashboard` → Kanbanタブ\n"
        f"- CLI: `hermes kanban show {task_id}`\n"
        f"- ウォッチ: `hermes kanban watch`\n\n"
        f"**Steering:** 実行中に計画を変更: `/ulw-steer add {session_id} <title>`"
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
