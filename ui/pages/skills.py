"""
ui.pages.skills - DEPRECATED backward-compatibility shim.

The "skill" terminology is now reserved for the standardized skills library;
assistant management lives in ui.pages.assistants (page_assistants).
"""
from ui.pages.assistants import page_assistants, _get_model_max_tokens_limit

# Legacy alias (old "skill" terminology).
page_skills = page_assistants

__all__ = ["page_skills", "page_assistants", "_get_model_max_tokens_limit"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
