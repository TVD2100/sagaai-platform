"""python -m sagaai - one of the two identical entry points.

The console is reachable in two ways and both must behave the same: the
``sagaai`` script on PATH (``bin/sagaai``) and this module. They agree by
construction - both call :func:`sagaai.cli.main` - so a difference between
them would be a bug in the launcher, not in the command.
"""

from __future__ import annotations

import sys

from sagaai.cli import main

if __name__ == "__main__":
    sys.exit(main())
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
