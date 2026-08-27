"""
core.skills - DEPRECATED backward-compatibility shim.

Historical name of the assistant management API. The "skills" terminology is
now reserved for the standardized skills library (core.skills_library), and
all user-facing AI profiles are called assistants (see core.assistants).

New code must import from core.assistants. The functions below are
thin re-exports kept so older modules (and third-party imports) keep working.
"""
from core.assistants import (
    ensure_dir,
    load_assistants_index,
    load_assistant_prompt_text,
    save_assistant_prompt_text,
    get_assistant_by_id,
    create_assistant,
    update_assistant,
    delete_assistant,
    get_assistant_files_dir,
    list_assistant_files,
    save_assistant_file,
    delete_assistant_file,
    load_assistant_files_context,
    SYSTEM_PROMPTS_DIR,
)

# ─── Legacy aliases (skill terminology) ───────────────────────────────────

load_prompts_index = load_assistants_index
load_prompt_text = load_assistant_prompt_text
save_prompt_text = save_assistant_prompt_text
get_prompt_by_id = get_assistant_by_id
create_skill = create_assistant
update_skill = update_assistant
delete_skill = delete_assistant
get_skill_files_dir = get_assistant_files_dir
list_skill_files = list_assistant_files
save_skill_file = save_assistant_file
delete_skill_file = delete_assistant_file
load_skill_files_context = load_assistant_files_context
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
