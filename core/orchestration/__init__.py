# -*- coding: utf-8 -*-
"""
core.orchestration - multi-agent orchestration contract and runtime.

Pure business logic: this package MUST NOT import streamlit or read
``st.session_state``. UI and HTTP layers call the public contract
(``RunService``) and render events; lifecycle ownership lives here.

The package holds four modules:

- ``contracts`` - transport-neutral dataclasses and the ``RunService``
  protocol (agent brief, spawn/invoke requests, run result, cancel report);
- ``service`` - the run registry, limits, allowlist enforcement and
  synchronous parallel fan-out;
- ``invoke`` - the one-shot assistant call (``invoke_once``) that
  deliberately disables assistant RAG, attachment files and the tool loop;
- ``tools`` - the ``spawn_agent`` / ``invoke_assistant`` catalog entries
  exposed to the model.
"""

from __future__ import annotations

__all__ = [
    "contracts",
    "invoke",
    "service",
    "tools",
]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
