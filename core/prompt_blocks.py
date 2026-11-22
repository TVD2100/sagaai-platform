# -*- coding: utf-8 -*-
"""Static prompt fragments and date formatters shared by orchestrator prompts.

The blocks here are pure text and pure functions: they depend on no repository,
no configuration and no session state, so they can live outside
``core.orchestrators`` without changing how a prompt is assembled. The names
are re-exported from that module, which stays the single entry point for prompt
building.
"""
from __future__ import annotations

from datetime import datetime

# ─── Agent-dialog rules ──────────────────────────────────────────────────────

# Kept as module constants rather than being assembled from fragments: the
# text is read by every orchestrator and a missing sentence changes behaviour.
DIALOG_RULES_PARENT = (
    "## Answering a subagent's question\n",
    "",
    "A subagent can park its run and ask you a question when the brief does "
    "not let it continue: it cannot reach the user, only you can answer.\n",
    "",
    "- The question arrives with `await_reason: question` and a `question` "
    "object. Answer it with `answer_child(run_id, answer)`; the subagent "
    "continues the SAME run, with the same files, context and step budget.\n",
    "- If you do not know the answer, ask the user in the dialog first, then "
    "pass their decision on. Never guess on the user's behalf.\n",
    "- Be concrete: the subagent acts on your words. An answer is a decision "
    "or a missing fact - not a new briefing - a new scope needs a new "
    "delegation.\n",
    "- The number of question/answer pairs per run is limited. When the "
    "subagent keeps asking instead of working, reply with a directive to "
    "finish with the information it already has.\n",
)

DIALOG_RULES_CHILD = (
    "## Asking the agent that launched you\n",
    "",
    "When the brief does not let you continue - a decision only the parent "
    "can make, a missing fact, an ambiguity that changes the result - park "
    "your run with `ask_parent`.\n",
    "",
    "- Put the decision you need in `question`, the answers you can act on "
    "in `options`, what is already finished in `done` and what you cannot do "
    "without the answer in `blocked`.\n",
    "- Asking parks the run: never start solving the same question in the "
    "same step, and never repeat a question you already asked.\n",
    "- The answer arrives as a user message and your work continues from "
    "where it stopped, with the same files and step budget.\n",
    "- Ask only for what you cannot decide yourself: the number of "
    "question/answer pairs per run is limited, and a question costs a round.\n",
)

# ─── Task-journal rules ─────────────────────────────────────────────────────

# The task journal (task_state) is a platform capability, not a role feature:
# every employee that has the tools receives the same rule about WHEN a
# journal is worth creating. Keeping the rule here rather than in each prompt
# is what stops the capability from existing with a rule in one role and no
# rule in the others - and what keeps a one-step request from paying for a
# plan, a file and a close it does not need.

TASK_JOURNAL_RULES_PARENT = (
    "## Task journal (task_state)\n",
    "",
    "The task journal is a working file for ONE task of this dialog. It is "
    "not a log of everything you do: creating it costs a file, a plan and a "
    "close, so a one-step request must not pay for it.\n",
    "",
    "Start a journal with `task_state_init` when BOTH hold:\n",
    "- the work needs 3 or more steps, or touches 3 or more files or units, "
    "or is a series of approved items;\n",
    "- the result is assembled from several findings, so a truncated dialog "
    "would lose the plan, the decisions or the handoff to a later step.\n",
    "",
    "Do NOT start a journal when:\n",
    "- the request is informational - a question, an explanation, a lookup, "
    "an opinion, a comparison: the answer itself is the result;\n",
    "- the change is one step - update one field, rename one file, fix one "
    "line, mark one project done;\n",
    "- the work ends inside one or two tool calls, in this same step.\n",
    "",
    "For those, act directly, then record the durable outcome in your "
    "long-term memory (`memory_append`) - a registry row or a memory entry "
    "is the right record of a finished action, and a task journal would only "
    "duplicate it.\n",
    "",
    "While a journal exists, keep it current: mark a step `in_progress` "
    "BEFORE the work on it and `done` AFTER its verification passed, with the "
    "facts the next step needs. The injected CURRENT TASK STATE block is the "
    "working view; the file is the record that survives the dialog. After the "
    "final report, close the task with `task_state_clear` - never delete the "
    "file.\n",
    "",
    "Write the journal for a reader who has only the journal. A step marked "
    "`done` carries a `verification` line naming the command and its result - "
    "an unproven step is recorded as an unproven step, not as done. `result` "
    "states what changed and `context` what the next step must know; both "
    "stay short enough to be read on their own. Working files of a task go "
    "into its `task_dir`, and the plan text, the verification lines and that "
    "folder are what the archive of a finished task keeps.\n",
)

TASK_JOURNAL_RULES_CHILD = (
    "## Task journal (task_state)\n",
    "",
    "Your journal is your OWN file: the caller reads it with "
    "`read_subagent_state` when it needs your handoff. Create it with "
    "`task_state_init` when the brief needs 3 or more steps or several files; "
    "skip it for a one-step lookup or a single edit - then just return the "
    "result.\n",
    "",
    "Record what a resume would need: the plan, the decisions, the files "
    "produced and what is left open. Never put the final answer only in the "
    "journal - the result travels in your reply, and the journal is the "
    "route back into unfinished work.\n",
    "",
    "Mark a step `in_progress` before the work and `done` after its "
    "verification, and close the task with `task_state_clear` once you send "
    "the result. A `done` step carries a `verification` line naming the "
    "command and its result: marking an unproven step as done tells the "
    "caller the work is finished when it is not.\n",
)

# ─── Delegation blocks ──────────────────────────────────────────────────────

#: Opening of the ``spawn_agent`` catalog block: what the tool does, how the
#: request is shaped and how a background child is collected.
SPAWN_AGENT_INTRO = [
    "## Available agents (spawn_agent)",
    "",
    "You may delegate a full agent task to these orchestrators. Pass "
    "the slug in `agent_slug` and describe the task in `request.goal` "
    "plus `request.brief`; every path you mention must be absolute:",
    "",
    "Add `background=true` to start a child and continue your own work: "
    "the call returns its `run_id` at once, and the finish arrives as a "
    "`[SUBAGENT_NOTICE:<run_id>:<status>]` system notice in a later "
    "step. Check it with `subagent_status(run_id=...)`, read the "
    "handoff with `read_subagent_state(run_id=...)` and continue "
    "unfinished work with `resume_subagent(run_id=..., "
    "request={goal, brief})` - omit `request` to continue the original "
    "goal from the child journal. A child that stopped at "
    "`awaiting_input`, `error`, `timeout` or `max_steps` has NOT "
    "finished the job. When you have nothing else to do while a "
    "background child runs, call `sleep(seconds=...)` instead of "
    "polling `subagent_status`: it returns as soon as a child "
    "finishes, or when the timeout expires. A status is not a "
    "result: collect what the child produced with "
    "`subagent_result(run_id=...)`, which returns the same result "
    "object a blocking spawn does, and answers `ready: false` with "
    "no result while the run is unfinished.",
    "",
]

#: Opening of the ``invoke_assistant`` catalog block.
INVOKE_ASSISTANT_INTRO = [
    "## Available assistants (invoke_assistant)",
    "",
    "One-shot model calls, no tools and no agent loop. Pass the id in "
    "`assistant_id`. Send a document as a file in `context_files` with "
    "an absolute path - one file, one call - instead of pasting its "
    "text or splitting it into several calls:",
    "",
]

#: Opening of the ``verify_result`` block.
VERIFY_RESULT_INTRO = [
    "## Verifying delegated work",
    "",
    "A fan-out gives you several results in one step, but not a check "
    "that they are right. When a fan-out returned two or more results "
    "and the outcome matters, call `verify_result(run_id=..., "
    "criteria=...)` for the result you intend to rely on, before you "
    "report it as done. State the criteria concretely: the verifier "
    "sees only the result and the artifacts, never the reasoning that "
    "produced them. If the verdict is `inconclusive`, say so instead "
    "of presenting the result as verified.",
]

# ─── Calendar names ─────────────────────────────────────────────────────────

# Weekday and month names are spelled out instead of using strftime("%A") so
# the block does not depend on the process locale.
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")
# Russian weekday names are returned by the ``current_datetime`` tool: models
# answer users in Russian and translating the name themselves is a needless
# chance to slip.
WEEKDAY_NAMES_RU = ("понедельник", "вторник", "среда", "четверг", "пятница",
                    "суббота", "воскресенье")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November",
               "December")


def format_utc_offset(moment: datetime) -> str:
    """Return the UTC offset of *moment* as ``UTC+HH:MM``."""
    offset = moment.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return "UTC%s%02d:%02d" % (sign, total // 3600, (total % 3600) // 60)


def current_date_line(moment: datetime) -> str:
    """Return the one-line date statement for *moment*.

    The weekday and the month are spelled out so the model does not derive
    them from a numeric date - a frequent source of off-by-one errors.
    """
    return "Today is %s, %02d %s %d." % (
        WEEKDAY_NAMES[moment.weekday()],
        moment.day,
        MONTH_NAMES[moment.month - 1],
        moment.year,
    )
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
