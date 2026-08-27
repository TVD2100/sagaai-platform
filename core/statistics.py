# -*- coding: utf-8 -*-
"""
core.statistics - token usage aggregation for the Statistics page.

Collects per-message token usage from BOTH databases:
  * regular chat threads  - main DB via ``core.threads`` (assistant dialogs).
    Provider attribution comes from ``Assistant.service``.
  * orchestrator threads  - isolated ``devagent.db`` via
    ``core.threads_devagent`` (DevAgent / employee dialogs). Provider
    attribution comes from the orchestrator config's ``strong_service``.

Message token payloads (``_tokens`` = {"in", "out", "cache"}) are embedded
in the message content as a JSON prefix and restored by the load functions
of both thread modules. Every message also carries an ISO ``ts`` timestamp.

Pure aggregation helpers (``_bucketize`` / ``build_summary``) take plain
record dicts, so unit tests can exercise the math without a database.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# ─── Public constants ────────────────────────────────────────────────────────

GRANULARITIES = ("day", "week", "month", "period")


# ─── Small pure helpers ──────────────────────────────────────────────────────

def _parse_ts(ts: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp into a naive datetime.

    Stored values look like ``2026-08-23T09:30:49.100474``. Returns None for
    None/empty/unparseable input so statistics silently skip broken rows.
    """
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _bucket_key(dt: datetime, granularity: str) -> str:
    """Return the grouping key for *dt* at the given granularity.

    day    -> ``YYYY-MM-DD``
    week   -> ``YYYY-MM-DD`` of the Monday starting that ISO week
    month  -> ``YYYY-MM``
    period -> the single bucket ``"period"``
    """
    if granularity == "day":
        return dt.strftime("%Y-%m-%d")
    if granularity == "week":
        monday = dt.date() - timedelta(days=dt.weekday())
        return f"{monday.isoformat()} (week)"
    if granularity == "month":
        return dt.strftime("%Y-%m")
    return "period"


def _int(v: Any) -> int:
    """Coerce a value to a non-negative int (0 on missing/invalid)."""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def cache_pct(cache: int, tokens_in: int) -> int:
    """Return the cached-input share in percent (0-100).

    When no input tokens were consumed the percentage is 0. Values above
    100% (rare provider rounding) are clamped, matching the chat token line.
    """
    if tokens_in <= 0:
        return 0
    return min(100, int(round(cache * 100.0 / tokens_in)))


# ─── Record collection ───────────────────────────────────────────────────────

def _message_tokens(msg: Dict[str, Any]) -> Tuple[int, int, int]:
    """Return (in, out, cache) from a restored message's ``_tokens``."""
    tokens = msg.get("_tokens") or {}
    if not isinstance(tokens, dict):
        return 0, 0, 0
    return _int(tokens.get("in")), _int(tokens.get("out")), _int(tokens.get("cache"))


def _chat_provider_map() -> Dict[str, str]:
    """Return {assistant_id: service} for all assistants (may be empty)."""
    try:
        from core.assistants import load_assistants_index
        return {
            str(a.get("id")): str(a.get("service") or "")
            for a in load_assistants_index()
            if a.get("id")
        }
    except Exception:
        return {}


def _orchestrator_provider_map() -> Dict[str, str]:
    """Return {slug: strong_service} for all orchestrators (may be empty).

    Attribution is per-thread and uses the primary ("strong") service, the
    model that does the bulk of the work; weak/search calls are minor.
    """
    try:
        from core.orchestrators import list_orchestrators
    except Exception:
        return {}
    result: Dict[str, str] = {}
    try:
        for orch in list_orchestrators():
            slug = str(orch.get("slug") or "")
            if not slug:
                continue
            cfg = orch.get("config") or {}
            service = str(cfg.get("strong_service") or cfg.get("service") or "")
            result[slug] = service or "unknown"
    except Exception:
        pass
    return result


def collect_chat_records() -> List[Dict[str, Any]]:
    """Collect token records from regular chat threads (main DB).

    Returns one dict per assistant message that carries ``_tokens``:
    ``{"ts": datetime, "in", "out", "cache", "provider", "source"}``.
    Empty list on any DB error (stale schema, locked DB, ...).
    """
    records: List[Dict[str, Any]] = []
    try:
        from core.threads import list_chat_threads, get_thread_messages
        providers = _chat_provider_map()
        for thread in list_chat_threads():
            tid = thread.get("thread_id")
            if not tid:
                continue
            provider = providers.get(str(thread.get("assistant_id") or "")) or "unknown"
            for msg in get_thread_messages(tid):
                tokens_in, tokens_out, tokens_cache = _message_tokens(msg)
                if tokens_in or tokens_out or tokens_cache:
                    dt = _parse_ts(msg.get("ts"))
                    if dt is None:
                        continue
                    records.append({
                        "ts": dt, "in": tokens_in, "out": tokens_out,
                        "cache": tokens_cache, "provider": provider,
                        "source": "chat",
                    })
    except Exception:
        records = []
    return records


def collect_orchestrator_records() -> List[Dict[str, Any]]:
    """Collect token records from orchestrator threads (devagent.db)."""
    records: List[Dict[str, Any]] = []
    try:
        from core.threads_devagent import list_devagent_threads, load_thread_messages
        providers = _orchestrator_provider_map()
        for thread in list_devagent_threads():
            tid = thread.get("thread_id")
            if not tid:
                continue
            slug = str(thread.get("assistant_id") or "")
            provider = providers.get(slug) or str(thread.get("assistant_name") or "") or "unknown"
            for msg in load_thread_messages(tid):
                tokens_in, tokens_out, tokens_cache = _message_tokens(msg)
                if tokens_in or tokens_out or tokens_cache:
                    dt = _parse_ts(msg.get("ts"))
                    if dt is None:
                        continue
                    records.append({
                        "ts": dt, "in": tokens_in, "out": tokens_out,
                        "cache": tokens_cache, "provider": provider,
                        "source": "orchestrator",
                    })
    except Exception:
        records = []
    return records


def collect_usage(start_dt: Optional[datetime] = None,
                  end_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Collect all token records within [start_dt, end_dt] (both bounds optional)."""
    records = collect_chat_records() + collect_orchestrator_records()
    if start_dt is not None:
        records = [r for r in records if r["ts"] >= start_dt]
    if end_dt is not None:
        records = [r for r in records if r["ts"] <= end_dt]
    records.sort(key=lambda r: r["ts"])
    return records


def available_bounds() -> Tuple[Optional[datetime], Optional[datetime]]:
    """Return (min_ts, max_ts) of all stored messages, or (None, None) when
    there is no usage data at all.

    Used by the page to compute a sane "all time" period.
    """
    records = collect_chat_records() + collect_orchestrator_records()
    if not records:
        return None, None
    ts_list = [r["ts"] for r in records]
    return min(ts_list), max(ts_list)


# ─── Aggregation ─────────────────────────────────────────────────────────────

def _bucketize(records: List[Dict[str, Any]], granularity: str) -> Dict[str, List[Dict[str, Any]]]:
    """Group *records* by the requested granularity. Sorted bucket keys are
    inserted in ascending order (dicts preserve insertion order in Python)."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        key = _bucket_key(rec["ts"], granularity)
        buckets.setdefault(key, []).append(rec)
    sorted_buckets = {}
    for key in sorted(buckets):
        sorted_buckets[key] = buckets[key]
    return sorted_buckets


def _bucket_rows(records: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    """Build one summary row per time bucket (for charts and tables)."""
    rows: List[Dict[str, Any]] = []
    for key, recs in _bucketize(records, granularity).items():
        tokens_in = sum(_int(r["in"]) for r in recs)
        tokens_out = sum(_int(r["out"]) for r in recs)
        tokens_cache = sum(_int(r["cache"]) for r in recs)
        rows.append({
            "key": key,
            "label": key.replace(" (week)", ""),
            "count": len(recs),
            "in": tokens_in,
            "out": tokens_out,
            "cache": tokens_cache,
            "total": tokens_in + tokens_out,
            "cache_pct": cache_pct(tokens_cache, tokens_in),
        })
    return rows


def build_summary(records: List[Dict[str, Any]], granularity: str,
                  start_dt: Optional[datetime] = None,
                  end_dt: Optional[datetime] = None) -> Dict[str, Any]:
    """Aggregate token records into the page's summary payload.

    Returns::

        {
            "granularity": str,
            "period": {"start": iso|None, "end": iso|None},
            "total": {"in", "out", "cache", "total", "cache_pct", "messages"},
            "buckets": [ row, ... ],          # empty for period granularity
            "providers": [ provider row by total desc ],
            "provider_names": [ ... same order ... ],
        }

    Provider rows: {"provider", "in", "out", "cache", "total", "cache_pct",
    "share_pct", "messages"}.
    """
    if granularity not in GRANULARITIES:
        granularity = "day"

    by_provider: Dict[str, Dict[str, Any]] = {}
    tokens_in = tokens_out = tokens_cache = 0
    for rec in records:
        ti, to, tc = _int(rec["in"]), _int(rec["out"]), _int(rec["cache"])
        tokens_in += ti
        tokens_out += to
        tokens_cache += tc
        provider = str(rec.get("provider") or "unknown")
        row = by_provider.setdefault(provider, {
            "provider": provider, "in": 0, "out": 0, "cache": 0,
            "messages": 0,
        })
        row["in"] += ti
        row["out"] += to
        row["cache"] += tc
        row["messages"] += 1

    total_tokens = tokens_in + tokens_out
    provider_rows = []
    for row in by_provider.values():
        row["total"] = row["in"] + row["out"]
        row["cache_pct"] = cache_pct(row["cache"], row["in"])
        row["share_pct"] = round(row["total"] * 100.0 / total_tokens, 1) if total_tokens else 0.0
        provider_rows.append(row)
    provider_rows.sort(key=lambda r: (-r["total"], r["provider"]))

    buckets: List[Dict[str, Any]] = []
    if granularity != "period":
        buckets = _bucket_rows(records, granularity)

    return {
        "granularity": granularity,
        "period": {
            "start": start_dt.isoformat() if start_dt else None,
            "end": end_dt.isoformat() if end_dt else None,
        },
        "total": {
            "in": tokens_in,
            "out": tokens_out,
            "cache": tokens_cache,
            "total": total_tokens,
            "cache_pct": cache_pct(tokens_cache, tokens_in),
            "messages": len(records),
        },
        "buckets": buckets,
        "providers": provider_rows,
        "provider_names": [r["provider"] for r in provider_rows],
    }
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
