# -*- coding: utf-8 -*-
"""Isolation backends of the execution sandbox.

A provider only runs a prepared request; the decision whether a run may
happen at all belongs to ``core.sandbox.policy``. The default set is built
here so a caller asks for "what this machine can do" instead of enumerating
backends itself.
"""
from __future__ import annotations

from typing import List

from core.sandbox.providers.bwrap import BwrapProvider
from core.sandbox.providers.container import ContainerProvider
from core.sandbox.providers.process import ProcessProvider
from core.sandbox.providers.seatbelt import SeatbeltProvider
from core.sandbox.providers.wsl import WslProvider


def default_providers(subprocess_module=None) -> List:
    """Return the providers this build knows about.

    The process provider is always present: it is the only backend that has
    no external dependency. Stronger backends are listed next to it and
    their probe decides availability at run time, so the policy picks the
    strongest one this machine can actually honour.

    ``subprocess_module`` is forwarded to the process provider so a caller
    that already owns a spawn module (the tool layer's dependency bundle,
    which tests replace) keeps using it instead of the global one.
    """
    return [ProcessProvider(subprocess_module=subprocess_module),
            SeatbeltProvider(subprocess_module=subprocess_module),
            BwrapProvider(subprocess_module=subprocess_module),
            WslProvider(subprocess_module=subprocess_module),
            ContainerProvider(subprocess_module=subprocess_module)]

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
