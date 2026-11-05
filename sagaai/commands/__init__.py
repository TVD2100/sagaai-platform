"""
sagaai.commands - one module per command, imported on demand.

Every module follows the same contract:

    SUMMARY     - one line for the command list in ``sagaai --help``
    DESCRIPTION - the long text of ``sagaai <command> --help``
    register(parser, add_global_flags) - declare arguments and actions
    execute(args, console) -> int           - perform the command

``register`` adds the ``--json`` flag to the command itself and to each of
its actions through the callback it is given; ``execute`` returns an exit
code from :mod:`sagaai.exit_codes` and formats its report through the
``Console``. A command never calls ``sys.exit`` and never prints a traceback -
both are the job of :func:`sagaai.cli.main`.

A module imports ``core.gateway`` at the top and its transport dependency
(uvicorn, streamlit) inside the function that needs it, so ``--help`` works on
an installation where the optional packages are absent.
"""

from __future__ import annotations
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
