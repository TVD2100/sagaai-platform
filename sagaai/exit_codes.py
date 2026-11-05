"""
sagaai.exit_codes - the documented outcome of every command.

Four codes, one meaning each, shared by every command. A script that checks
the code must not need to know which command produced it: "2" is always "ask
the operator to fix the configuration", never "the database is locked" for one
command and "bad argument" for another.

    OK       0  the operation was performed
    FAILURE  1  the operation was attempted and did not succeed
    CONFIG   2  the request or the installation is not configured for it
    CONFLICT 3  another process or a live run holds the resource

The split between FAILURE and CONFIG is the one that matters in practice: a
cron job should stop and page a human on CONFIG (the next attempt will fail
the same way) but may retry a FAILURE.
"""

from __future__ import annotations

OK = 0
FAILURE = 1
CONFIG = 2
CONFLICT = 3

#: Human wording of every code, used in --help epilogs and in error reports.
MEANING = {
    OK: "success",
    FAILURE: "the operation failed",
    CONFIG: "the configuration or the request is invalid",
    CONFLICT: "another instance or a live run holds the resource",
}

#: The block every command's --help ends with. Kept in one place so a command
#: cannot describe the codes differently from the others.
HELP_EPILOG = (
    "exit codes:\n"
    "  0  success\n"
    "  1  the operation failed\n"
    "  2  the configuration or the request is invalid\n"
    "  3  another instance or a live run holds the resource"
)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
