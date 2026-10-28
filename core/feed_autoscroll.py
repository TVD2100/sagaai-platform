# -*- coding: utf-8 -*-
"""core.feed_autoscroll - client-side auto-scroll for the orchestrator chat feed.

While the agent works the chat feed grows at the bottom of the page, but the
pinned bottom bar (folder picker + chat input + toolbar) stays on screen, so new
output easily slips out of view.  This module emits a tiny JavaScript snippet
that:

* keeps the feed scrolled to the newest message while the agent is active;
* pauses that follow as soon as the user scrolls up to read earlier messages;
* resumes it once the user scrolls back to the bottom.

Streamlit 1.66 already reserves room for the pinned bottom bar, so the module
adds no padding of its own - doing so produced a large empty gap between the last
message and the input form.

The pause flag lives in the parent tab's ``sessionStorage``, so it survives the
component iframe being recreated on every Streamlit rerun.  No user-facing
strings live here - the caller renders all text.
"""
from __future__ import annotations

# Distance from the feed bottom (px) still treated as "the user is at the
# bottom".  A manual scroll further up pauses follow mode.
AUTOSCROLL_THRESHOLD_PX = 150

_THRESHOLD_PLACEHOLDER = "__THRESHOLD_PX__"
_FORCE_PLACEHOLDER = "__FORCE_FLAG__"

# The template is a plain (non-f) string on purpose: the client code is full of
# braces, so placeholders are substituted by ``autoscroll_js`` instead.
AUTOSCROLL_TEMPLATE = """<script>
(function () {
    try {
        var win = window.parent;
        var doc = win.document;
        var THRESHOLD = __THRESHOLD_PX__;
        var FORCE = __FORCE_FLAG__;
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

        function scroller() {
            var sels = ['[data-testid="stMain"]', 'section.main',
                        '[data-testid="stAppViewContainer"]'];
            for (var i = 0; i < sels.length; i++) {
                var el = doc.querySelector(sels[i]);
                if (el && el.scrollHeight > el.clientHeight) { return el; }
            }
            return doc.scrollingElement || doc.documentElement;
        }
        function atBottom(el) {
            return (el.scrollHeight - el.scrollTop - el.clientHeight) <= THRESHOLD;
        }
        function scrollDown() {
            var el = scroller();
            if (!el) { return; }
            el.scrollTop = el.scrollHeight;
            win.setTimeout(function () { el.scrollTop = el.scrollHeight; }, 50);
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


def autoscroll_js(force: bool = False) -> str:
    """Return the feed auto-scroll ``<script>`` fragment.

    Parameters
    ----------
    force:
        When True the pause flag is cleared and the feed is scrolled to the
        bottom unconditionally (used right after the user sends a message).

    Returns
    -------
    str
        A self-contained ``<script>...</script>`` fragment for
        ``streamlit.components.v1.html``.
    """
    return (AUTOSCROLL_TEMPLATE
            .replace(_THRESHOLD_PLACEHOLDER, str(AUTOSCROLL_THRESHOLD_PX))
            .replace(_FORCE_PLACEHOLDER, "true" if force else "false"))


def render_autoscroll(force: bool = False) -> None:
    """Emit the auto-scroll script into the current Streamlit render.

    Parameters
    ----------
    force:
        Force a scroll to the bottom, ignoring a manual pause.
    """
    import streamlit.components.v1 as components
    components.html(autoscroll_js(force), height=0, scrolling=False)
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
