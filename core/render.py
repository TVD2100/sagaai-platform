"""
core.render - markdown rendering helpers.
clipboard_button requires streamlit but is kept here for API compatibility
(it renders via st.html so the button inherits the active Streamlit theme);
_md_to_html, _md_to_txt, _iter_md_blocks are pure Python.
"""
import json
import re
import html as html_lib
from typing import Dict, Optional


def clipboard_button(text: str, key: str, label: str = "📋 MD",
                     copy_url_params: Optional[Dict[str, str]] = None):
    """Render a compact theme-aware clipboard-copy button via ``st.html``.

    The previous implementation used ``streamlit.components.v1.html``, which
    renders inside an isolated iframe that does NOT inherit the Streamlit
    theme. The button therefore relied on ``color: inherit`` inside an iframe
    without a text colour, making the label invisible in dark mode.
    ``st.html(unsafe_allow_javascript=True)`` renders the same markup in the
    main document, so the styles below use Streamlit's theme CSS variables
    (``--background-color``, ``--secondary-background-color``,
    ``--text-color``, ``--border-color``) and stay legible in every theme.

    Two modes:

    * Text copy (default): the supplied ``text`` is copied to the clipboard.
    * URL copy: pass ``copy_url_params`` (a dict of query parameters). The
      JavaScript then builds the CURRENT page URL (origin + pathname), sets
      the given parameters and copies the resulting deep link. ``text`` is
      ignored in this mode.

    The clipboard payload is embedded as a JSON-encoded ``data-clip``
    attribute and never interpolated into the JS source directly, so
    arbitrary message content (HTML tags, quotes, newlines, script fragments)
    cannot break the button markup.
    """
    import streamlit as st  # noqa: imported at call time

    data_clip = html_lib.escape(json.dumps(text, ensure_ascii=False), quote=True)
    data_params = (
        html_lib.escape(json.dumps(copy_url_params, ensure_ascii=False), quote=True)
        if copy_url_params is not None else ""
    )
    data_label = html_lib.escape(label, quote=True)
    label_html = html_lib.escape(label)
    unique_id = "cb_" + re.sub(r"[^A-Za-z0-9_-]", "_", key)

    st.html(
        f"""
        <style>
          .cb-btn {{
            background: transparent;
            border: 1px solid var(--border-color, rgba(49,51,63,0.2));
            border-radius: 8px;
            padding: 4px 10px;
            font-size: 0.78rem;
            font-family: var(--font, inherit);
            cursor: pointer;
            color: var(--text-color, #262730);
            width: 100%;
            transition: background 0.15s;
          }}
          .cb-btn:hover {{ background: rgba(128,128,128,0.15); }}
          .cb-btn.copied {{ color: #22c55e; border-color: #22c55e; }}
        </style>
        <button class="cb-btn" id="{unique_id}" data-clip="{data_clip}"
                data-params="{data_params}" data-label="{data_label}">{label_html}</button>
        <script>
        (function() {{
            var btn = document.getElementById('{unique_id}');
            if (!btn) {{ return; }}
            var text = JSON.parse(btn.getAttribute('data-clip'));
            var paramsAttr = btn.getAttribute('data-params');
            var params = paramsAttr ? JSON.parse(paramsAttr) : null;
            var label = btn.getAttribute('data-label');
            function restore() {{
                btn.textContent = label;
                btn.classList.remove('copied');
            }}
            function fallbackCopy(value) {{
                var ta = document.createElement('textarea');
                ta.value = value;
                document.body.appendChild(ta);
                ta.select();
                try {{ document.execCommand('copy'); }} catch (e) {{}}
                document.body.removeChild(ta);
            }}
            btn.addEventListener('click', function() {{
                var value = text;
                if (params) {{
                    var url = new URL(window.location.href);
                    Object.keys(params).forEach(function(k) {{
                        url.searchParams.set(k, params[k]);
                    }});
                    url.hash = '';
                    value = url.toString();
                }}
                function done() {{
                    btn.textContent = '✅';
                    btn.classList.add('copied');
                    setTimeout(restore, 1500);
                }}
                if (navigator.clipboard && navigator.clipboard.writeText) {{
                    navigator.clipboard.writeText(value).then(done).catch(function() {{
                        fallbackCopy(value);
                        done();
                    }});
                }} else {{
                    fallbackCopy(value);
                    done();
                }}
            }});
        }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )


def _md_to_html(text: str) -> str:
    """Convert Markdown to a self-contained HTML document string."""
    try:
        import markdown
        body = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
    except Exception:
        body = "<pre>" + html_lib.escape(text) + "</pre>"
    return f"""
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <style>
        body {{ font-family: Arial, sans-serif; font-size: 11pt; line-height: 1.55; color: #222; }}
        h1 {{ font-size: 20pt; margin: 0 0 10px; }}
        h2 {{ font-size: 16pt; margin: 16px 0 8px; }}
        h3 {{ font-size: 13pt; margin: 14px 0 6px; }}
        p {{ margin: 0 0 10px; }}
        ul, ol {{ margin: 0 0 10px 20px; }}
        li {{ margin: 0 0 4px; }}
        pre {{ background: #f5f5f5; border: 1px solid #e5e5e5; padding: 10px; border-radius: 6px; white-space: pre-wrap; }}
        code {{ background: #f5f5f5; padding: 2px 4px; border-radius: 4px; }}
        blockquote {{ border-left: 4px solid #d0d0d0; margin: 8px 0; padding: 4px 0 4px 12px; color: #555; }}
        table {{ border-collapse: collapse; width: 100%; margin: 8px 0 12px; }}
        th, td {{ border: 1px solid #dcdcdc; padding: 6px 8px; vertical-align: top; }}
        th {{ background: #f2f2f2; }}
      </style>
    </head>
    <body>{body}</body>
    </html>
    """


def _md_to_txt(text: str) -> str:
    """Strip Markdown markup and return plain text."""
    t = text or ""
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"```[\s\S]*?```", "", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s{0,3}>\s?", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\d+[.)]\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*\\\d+\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"\\\d+", "", t)
    t = re.sub(r"\\[A-Za-zА-Яа-я_]+", "", t)
    t = re.sub(r"[*_~]+", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    lines = [line.strip() for line in t.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def format_token_line(current_tokens: int, tokens_in: int = 0, tokens_out: int = 0,
                      economy_meta: str = "", color: str = "green",
                      tokens_cache: int = 0) -> str:
    """Build the single-line token usage indicator used by the chat pages.

    Returns an HTML snippet ready for ``st.markdown(..., unsafe_allow_html=True)``.
    Example rendered text::

        Context: current 7,849 / total: 25,328 (in 24,362 / cache 83% / out 966) 💡 economy (6/6 msgs)

    Args:
        current_tokens: token count currently in the model context.
        tokens_in: cumulative input tokens spent across all calls in the thread.
        tokens_out: cumulative output tokens spent across all calls in the thread.
        economy_meta: optional suffix such as "💡 economy (30/122 msgs)".
        color: CSS color for the "current" number (default green).
        tokens_cache: cumulative cached input tokens reported by the provider.
            When > 0, a ``cache <pct>%`` part is shown between ``in`` and
            ``out``, where pct = tokens_cache / tokens_in * 100.
    """
    total = int(tokens_in or 0) + int(tokens_out or 0)
    in_val = int(tokens_in or 0)
    cache_val = int(tokens_cache or 0)
    parts = f"in {in_val:,}"
    if cache_val > 0 and in_val > 0:
        cache_pct = int(round(cache_val * 100.0 / in_val))
        if cache_pct > 100:
            cache_pct = 100
        parts += f" / cache {cache_pct}%"
    parts += f" / out {int(tokens_out or 0):,}"
    html = (
        f"Context: current <span style=\"color:{color};font-weight:600\">"
        f"{int(current_tokens or 0):,}</span> "
        f"/ total: {total:,} "
        f"({parts})"
    )
    if economy_meta:
        html += f" {economy_meta}"
    return html


def _iter_md_blocks(text: str):
    """
    Yield (type, ...) tuples representing parsed Markdown blocks.
    Types: 'heading', 'code', 'ul', 'ol', 'table', 'blockquote', 'p'.
    """
    lines = text.splitlines()
    i = 0
    in_code = False
    code_buf = []
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if in_code:
                yield ("code", "\n".join(code_buf))
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            yield ("heading", len(m.group(1)), m.group(2).strip())
            i += 1
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            yield ("ul", items)
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip())
                i += 1
            yield ("ol", items)
            continue
        if (line.strip().startswith("|") and
                i + 1 < len(lines) and
                re.match(r"^\|?\s*[-: ]+(\|\s*[-: ]+)+\|?\s*$", lines[i + 1].strip())):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            yield ("table", header, rows)
            continue
        if line.strip().startswith(">"):
            quotes = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quotes.append(lines[i].strip()[1:].strip())
                i += 1
            yield ("blockquote", " ".join(quotes))
            continue
        if not line.strip():
            i += 1
            continue
        para = [line]
        i += 1
        while (i < len(lines) and lines[i].strip() and
               not re.match(r"^(#{1,6})\s+", lines[i]) and
               not re.match(r"^\s*[-*+]\s+", lines[i]) and
               not re.match(r"^\s*\d+\.\s+", lines[i]) and
               not lines[i].strip().startswith("|") and
               not lines[i].strip().startswith(">") and
               not lines[i].strip().startswith("```")):
            para.append(lines[i])
            i += 1
        yield ("p", " ".join(s.strip() for s in para).strip())
    if in_code and code_buf:
        yield ("code", "\n".join(code_buf))


def format_ts_label(ts_raw: str) -> str:
    """Format an ISO timestamp as a compact ``HH:MM DD.MM.YYYY`` label.

    The messages table stores ``datetime.now().isoformat()`` values such as
    ``2026-08-23T09:30:49.100474``; this helper renders the time first and
    the date after it (``09:30 23.08.2026``), matching the chat caption
    format used by the orchestrator and assistant chat pages.

    Returns an empty string for None, empty or unrecognisable input. When
    only one component (date or time) is present, returns that component
    (``23.08.2026`` / ``09:30``).
    """
    raw = (ts_raw or "").strip()
    if not raw:
        return ""
    date_m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    time_m = re.search(r"(?:^|[T\s])(\d{1,2}):(\d{2})", raw)
    if date_m and time_m:
        return (
            f"{int(time_m.group(1)):02d}:{int(time_m.group(2)):02d} "
            f"{date_m.group(3)}.{date_m.group(2)}.{date_m.group(1)}"
        )
    if date_m:
        return f"{date_m.group(3)}.{date_m.group(2)}.{date_m.group(1)}"
    if time_m:
        return f"{int(time_m.group(1)):02d}:{int(time_m.group(2)):02d}"
    return ""
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
