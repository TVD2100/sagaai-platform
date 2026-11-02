# -*- coding: utf-8 -*-
"""core.feed_autoscroll - client-side auto-scroll for the orchestrator chat feed.

While the agent works the chat feed grows at the bottom of the page, but the
pinned bottom bar (folder picker + chat input + toolbar) stays on screen, so new
output easily slips out of view.  This module emits a tiny JavaScript snippet
that:

* keeps the feed scrolled to the newest message while the agent is active;
* pauses that follow as soon as the user scrolls up to read earlier messages;
* resumes it once the user scrolls back to the bottom.

The snippet is injected with ``st.html(unsafe_allow_javascript=True)``, which
executes it in the main document.  An iframe component cannot reach the app's
scroll container, and ``st.components.v1.html`` is deprecated since Streamlit
1.66.

Streamlit 1.66 scrolls ``main[data-testid="stAppScrollToBottomContainer"]``
(the ``stMain`` id/class node): the testid there overrides ``stMain``, and the
``section.main`` class of older releases no longer exists.  The candidate list
below prefers the current testid and keeps the former selectors as fallbacks.

Streamlit 1.66 already reserves room for the pinned bottom bar, so the module
adds no padding of its own - doing so produced a large empty gap between the last
message and the input form.

The pause flag lives in the tab's ``sessionStorage``, so it survives the snippet
being re-injected on every Streamlit rerun.  No user-facing strings live here -
the caller renders all text.
"""
from __future__ import annotations

import itertools

# Distance from the feed bottom (px) still treated as "the user is at the
# bottom".  A manual scroll further up pauses follow mode.
AUTOSCROLL_THRESHOLD_PX = 150

_THRESHOLD_PLACEHOLDER = "__THRESHOLD_PX__"
_FORCE_PLACEHOLDER = "__FORCE_FLAG__"
_RENDER_PLACEHOLDER = "__RENDER_ID__"

# Streamlit re-runs an ``st.html`` script only when the injected body changes,
# so every emission carries its own render id (an always-changing comment or an
# attribute would be stripped by the sanitizer; a script variable survives).
_RENDER_SEQ = itertools.count(1)

# The template is a plain (non-f) string on purpose: the client code is full of
# braces, so placeholders are substituted by ``autoscroll_js`` instead.
AUTOSCROLL_TEMPLATE = """<script>
(function () {
    try {
        var win = window.parent;
        var doc = win.document;
        var THRESHOLD = __THRESHOLD_PX__;
        var FORCE = __FORCE_FLAG__;
        var RENDER_ID = __RENDER_ID__;
        var PAUSE_KEY = '__saga_feed_autoscroll_paused';

        var store = null;
        try { store = win.sessionStorage; } catch (e) { store = null; }
        function getPause() {
            try { return store ? store.getItem(PAUSE_KEY) === '1' : false; }
            catch (e) { return false; }
        }
        function setPause(on) {
            try { if (store) { store.setItem(PAUSE_KEY, on ? '1' : '0'); } }
            catch (e) {}
        }

        function scrollable(el) {
            if (!el || el.scrollHeight <= el.clientHeight) { return false; }
            var oy = '';
            try {
                var cs = win.getComputedStyle ? win.getComputedStyle(el) : null;
                oy = cs ? (cs.overflowY || '') : '';
            } catch (e) { oy = ''; }
            return oy === '' || oy === 'auto' || oy === 'scroll' || oy === 'overlay';
        }
        function scroller() {
            var sels = ['[data-testid="stAppScrollToBottomContainer"]',
                        '#stMain', '[data-testid="stMain"]',
                        'section.main',
                        '[data-testid="stAppViewContainer"]'];
            var first = null;
            for (var i = 0; i < sels.length; i++) {
                var el = doc.querySelector(sels[i]);
                if (!el) { continue; }
                if (!first) { first = el; }
                if (scrollable(el)) { return el; }
            }
            var root = doc.scrollingElement || doc.documentElement;
            if (scrollable(root)) { return root; }
            return first || root;
        }
        function atBottom(el) {
            return (el.scrollHeight - el.scrollTop - el.clientHeight) <= THRESHOLD;
        }
        function scrollDown() {
            var el = scroller();
            if (!el) { return; }
            el.scrollTop = el.scrollHeight;
            // The feed may still be growing when the script runs, so the jump
            // is repeated once the render has settled.
            win.setTimeout(function () { el.scrollTop = el.scrollHeight; }, 50);
            win.setTimeout(function () { el.scrollTop = el.scrollHeight; }, 300);
        }

        if (!win.__sagaFeedAutoscrollBound) {
            win.__sagaFeedAutoscrollBound = true;
            win.__sagaFeedLastTop = -1;
            var onScroll = function () {
                var el = scroller();
                if (!el) { return; }
                var top = el.scrollTop;
                var last = win.__sagaFeedLastTop;
                win.__sagaFeedLastTop = top;
                if (atBottom(el)) {
                    setPause(false);
                } else if (last >= 0 && top < last - 2) {
                    setPause(true);
                }
            };
            doc.addEventListener('scroll', onScroll, true);
            win.addEventListener('wheel', function (e) {
                if (e.deltaY < 0) {
                    var el = scroller();
                    if (el && !atBottom(el)) { setPause(true); }
                }
            }, { passive: true });
            win.addEventListener('touchmove', function () {
                var el = scroller();
                if (el && !atBottom(el)) { setPause(true); }
            }, { passive: true });
        }

        if (FORCE) { setPause(false); }
        if (FORCE || !getPause()) { scrollDown(); }
    } catch (e) {}
})();
</script>"""


def autoscroll_js(force: bool = False, render_id: int = 0) -> str:
    """Return the feed auto-scroll ``<script>`` fragment.

    Parameters
    ----------
    force:
        When True the pause flag is cleared and the feed is scrolled to the
        bottom unconditionally (used right after the user sends a message).
    render_id:
        Value embedded as the client-side ``RENDER_ID`` constant.  It only
        makes the body differ between emissions, which is what makes
        Streamlit re-run the script on the next rerun.

    Returns
    -------
    str
        A self-contained ``<script>...</script>`` fragment for
        ``st.html(unsafe_allow_javascript=True)``.
    """
    return (AUTOSCROLL_TEMPLATE
            .replace(_THRESHOLD_PLACEHOLDER, str(AUTOSCROLL_THRESHOLD_PX))
            .replace(_FORCE_PLACEHOLDER, "true" if force else "false")
            .replace(_RENDER_PLACEHOLDER, str(render_id)))


def render_autoscroll(force: bool = False) -> None:
    """Emit the auto-scroll script into the current Streamlit render.

    The snippet goes through ``st.html(unsafe_allow_javascript=True)`` so it
    executes in the main document: the deprecated ``st.components.v1.html``
    runs it inside a sandboxed iframe, which both logs a removal warning on
    every call and cannot drive the app's own scroll container.

    Parameters
    ----------
    force:
        Force a scroll to the bottom, ignoring a manual pause.
    """
    import streamlit as st

    st.html(autoscroll_js(force, render_id=next(_RENDER_SEQ)),
            unsafe_allow_javascript=True)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
