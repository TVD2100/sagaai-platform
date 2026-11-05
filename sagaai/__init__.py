"""
sagaai - the console transport of the SagaAI platform.

The package is a THIN layer over the core: a command parses arguments, calls
an existing core function and formats the answer. No business rule lives here
- if a command needs something the core cannot do, the function is added to
``core.gateway`` and covered by a test there.

Layering, enforced by ``tests/test_cli_imports.py``:

    sagaai/  ->  core.gateway  ->  core.orchestration  ->  dev_agent
    sagaai/  ->  core.updater, core.api_token_store, core.paths, ...
    sagaai serve  ->  core.gateway_launcher  ->  server/
    sagaai ui     ->  a child process running streamlit (never imported)

Two entry points exist and must stay identical:

    python -m sagaai        (sagaai/__main__.py)
    sagaai                  (bin/sagaai, put on PATH)

Both call :func:`sagaai.cli.main`, so the behaviour is the same by
construction and not by coincidence.
"""

from __future__ import annotations

#: Version of the console contract. Bumped when a command's arguments or its
#: JSON payload change in a way a script could notice. Independent of the
#: platform version, which travels in every JSON envelope.
__version__ = "1.0.0"


def main(argv=None) -> int:
    """Run the console entry point (re-exported for a one-import launcher)."""
    from sagaai.cli import main as _main
    return _main(argv)


__all__ = ["main", "__version__"]
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
