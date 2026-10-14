"""
core.rag_bm25 - own BM25F lexical index for RAG knowledge bases.

Every RAG base can be searched lexically (pure Python, no API keys). The
postings live in the base's existing local SQLite index
(``<base_dir>/index.db``) in two extra tables WITH a field dimension:

  bm25_terms(term TEXT, chunk_id INTEGER, field TEXT, tf INTEGER)
  bm25_docs(chunk_id INTEGER, field TEXT, dl INTEGER)

Two fields are indexed per chunk:

  * ``body`` - the chunk text with its self-describing header removed;
  * ``meta`` - the header values written by the RAG Base Creator
    (``Документ`` / ``Продукт`` / ``Заголовок`` / ``Раздел``).

Why two fields (BM25F). A header value repeated in EVERY chunk of a
document family (e.g. ``Продукт: Маркетинг``) is useless for ranking: when
body and header share one posting list its document frequency reaches N and
its IDF collapses, blinding the ranker on the corpus' own domain term. The
fielded layout fixes this and keeps the useful half:

  * each field has its OWN document frequency and average length, so a
t    repeated header term has df ~ N *within the meta field* and contributes
almost nothing there, while the domain term keeps a high IDF in the body
    field;
  * a product name that appears ONLY in the header (``Продукт: Yandex
    Atelier``) stays searchable, because it is still indexed in the meta
    field - without the field it was simply unfindable.

Scoring (BM25F, Lucene-style k1=1.5 / b=0.75, per-field statistics):

  idf(t,f)  = ln(1 + (N - df(t,f) + 0.5) / (df(t,f) + 0.5))
  score_f(d)= sum_t idf(t,f) * tf(t,d,f) * (k1 + 1)
              / (tf + k1 * (1 - b + b * dl(d,f) / avgdl(f)))
  score(d)  = w_body * score_body(d) + w_meta * score_meta(d)

N is the collection size, df/avgdl are taken per field, and w_body/w_meta
are the field weights in ``FIELD_WEIGHTS`` (both 1.0 by default).

Multi-term queries additionally apply a coordination factor
(``Coord = 0.5 + 0.5 * matched_terms / total_terms``) so a chunk matching
all meaningful query terms outranks a chunk matching only one of them.
Single-term queries are unaffected.

Text analysis:

  * tokenization is Unicode-aware (lowercase, ``ё`` folds to ``е``);
  * Cyrillic tokens are reduced to a conservative stem (a small
    inflectional-suffix stemmer, no third-party dependency), so the word
    forms ``бренд`` / ``бренда`` / ``брендов`` share one posting;
  * Latin tokens are left as they are (no risky English stemming);
  * a small Russian/English stop-word list removes function words
    (``как``, ``что``, ``для`` ...) from both indexing and queries;
  * the self-describing chunk header is split off the body and indexed as
    the separate ``meta`` field (see above); the STORED chunk text keeps
    the header, so the agent still reads a self-describing fragment.

Lifecycle:

  * a base indexed by an older version has no BM25 tables or carries an
    older ``bm25_version`` (or legacy tables without the ``field``
    column): the first search triggers one full rebuild (``ensure_ready``),
    after which the index is maintained incrementally by the hooks below
    (called from the core.rag_index write functions).

No streamlit imports; every function takes an explicit database path.
"""
import heapq
import math
import os
import re
import sqlite3


# Unicode word tokens: letters and digits of any script, underscore excluded.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Standard Lucene-style BM25 parameters (validated against the bundled
# bases: k1=1.5 / b=0.75 keeps the best top-1 hit among the tested grid).
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

# Schema version of the BM25 structures. Bump it whenever the text analysis,
# the field layout or the scoring changes: is_ready() then reports False for
# older indexes and the next search rebuilds them once (no silent stale
# index). v3 introduced the fielded (BM25F) layout.
SCHEMA_VERSION = 3

# Indexed fields. 'body' is the chunk text without its header; 'meta' is the
# header block (Документ/Продукт/Заголовок/Раздел).
FIELD_BODY = "body"
FIELD_META = "meta"
_FIELD_ORDER = (FIELD_BODY, FIELD_META)

# Per-field weights of the BM25F score. Both default to 1.0.
FIELD_WEIGHTS = {FIELD_BODY: 1.0, FIELD_META: 1.0}

# Coordination floor for multi-term queries: a chunk matching none of the
# extra terms keeps this share of its score (never 0, so recall is safe).
COORD_FLOOR = 0.5

# meta keys of the cached BM25 state (shared meta table). Document lengths are
# cached PER FIELD under 'bm25_len_<field>'.
_READY_KEY = "bm25_ready"
_VERSION_KEY = "bm25_version"
_DOCS_KEY = "bm25_docs_count"


def _len_key(field: str) -> str:
    """Return the meta key that caches a field's total token count."""
    return "bm25_len_" + str(field)


# ─── text analysis: metadata ─────────────────────────────────────────────────

# Header lines written by build_base.py (RAG Base Creator) into every chunk.
META_LINE_RE = re.compile(
    r"^(Документ|Продукт|Заголовок|Раздел|Фрагмент)\s*:\s*(.*)$", re.UNICODE)

# Header keys that feed the meta field. 'Фрагмент' is a pure position marker
# (1/3, 2/3 ...) and carries no ranking signal, so it is skipped.
_META_FIELD_KEYS = ("Документ", "Продукт", "Заголовок", "Раздел")


def split_metadata(text: str):
    """Split a chunk into its leading metadata block and the body text.

    Returns ``(body, meta)`` where *meta* maps header keys to their values
    (first occurrence wins) and *body* is the text with the leading header
    block removed. Chunks without a header are returned unchanged, so the
    function is safe for arbitrary user text as well.
    """
    raw = str(text or "")
    if not raw:
        return "", {}
    lines = raw.split("\n")
    meta: dict = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            idx += 1
            continue
        match = META_LINE_RE.match(stripped)
        if not match:
            break
        key = match.group(1)
        if key not in meta:
            meta[key] = match.group(2).strip()
        idx += 1
    if not meta:
        return raw, {}
    return "\n".join(lines[idx:]), meta


def _meta_text(meta: dict) -> str:
    """Concatenate the header values that feed the meta field."""
    parts = []
    for key in _META_FIELD_KEYS:
        val = str((meta or {}).get(key) or "").strip()
        if val:
            parts.append(val)
    return " ".join(parts)


# ─── text analysis: stemming ─────────────────────────────────────────────────

# Conservative inflectional suffixes (longest first). Only endings that
# change the form of the SAME word are listed; derivational suffixes are
# deliberately omitted so unrelated words are not merged.
_RU_SUFFIXES = tuple(sorted({
    # verbs, participles, gerunds
    "ироваться", "ировать", "овать", "евать", "ывать", "ивать",
    "аться", "яться", "иться", "еться", "аешь", "иешь", "уешь",
    "аете", "иете", "айте", "ийте", "ился", "илась", "илось",
    "ались", "ились", "елся", "ать", "ять", "еть", "ить",
    "уть", "ыть", "ешь", "ишь", "ете", "ите", "ем", "им",
    "ут", "ют", "ат", "ят", "ет", "ит", "ла", "ло", "ли", "л",
    # adjectives and participles
    "ейший", "айший", "ейшего", "ого", "его", "ому", "ему",
    "ыми", "ими", "ых", "их", "ый", "ий", "ой", "ая", "яя",
    "ое", "ее", "ые", "ие", "ую", "юю",
    # nouns
    "иями", "ями", "ами", "иях", "иям", "ием", "ией", "ии", "ия",
    "ию", "ие", "ей", "ою", "ею", "ов", "ев", "ах", "ях", "ам",
    "ям", "ом", "ем", "ою", "ью", "ья", "ье", "у", "ю", "а",
    "я", "о", "е", "ы", "и", "й", "ь",
}, key=lambda s: (-len(s), s)))

# Stemming only applies to tokens at least this long, and never shortens a
# token below _MIN_STEM characters. Both guards keep short words intact.
_MIN_STEM_WORD = 5
_MIN_STEM = 4

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def _has_cyrillic(token: str) -> bool:
    """True when *token* contains at least one Cyrillic letter."""
    return bool(_CYRILLIC_RE.search(token or ""))


def stem_token(token: str) -> str:
    """Reduce a token to a conservative stem.

    Cyrillic tokens are shortened by a listed inflectional suffix; Latin
    and numeric tokens are returned unchanged (English stemming is not
    attempted here). Guards keep the stem at least ``_MIN_STEM`` chars long
    so short words are never mangled.
    """
    word = str(token or "")
    if len(word) < _MIN_STEM_WORD or not _has_cyrillic(word):
        return word
    for suffix in _RU_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[:len(word) - len(suffix)]
    return word


# Function words dropped from indexing and queries. A curated subset of the
# classic Russian/English stop lists - no domain terms.
_STOPWORDS_RAW = (
    # Russian
    "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а",
    "то", "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же",
    "вы", "за", "бы", "по", "только", "ее", "мне", "было", "вот", "от",
    "меня", "еще", "нет", "о", "из", "ему", "теперь", "когда", "даже",
    "ну", "вдруг", "ли", "если", "уже", "или", "ни", "быть", "был", "него",
    "до", "вас", "вам", "ведь", "там", "потом", "себя", "ничего", "ей",
    "может", "они", "тут", "где", "есть", "надо", "ней", "для", "мы",
    "тебя", "их", "чем", "была", "сам", "чтоб", "без", "будто", "чего",
    "раз", "тоже", "себе", "под", "будет", "тогда", "кто", "этот", "того",
    "потому", "этого", "какой", "совсем", "ним", "здесь", "этом", "один",
    "почти", "мой", "тем", "чтобы", "нее", "сейчас", "были", "куда",
    "зачем", "всех", "никогда", "можно", "при", "наконец", "два", "об",
    "другой", "хоть", "после", "над", "больше", "тот", "через", "эти",
    "нас", "про", "всего", "них", "какая", "много", "разве", "три", "эту",
    "моя", "впрочем", "хорошо", "свою", "этой", "перед", "иногда", "лучше",
    "чуть", "том", "нельзя", "такой", "им", "более", "всегда", "конечно",
    "всю", "между", "это", "такое", "чему", "этих", "этими", "который",
    "которая", "которое", "которые", "наш", "ваш", "свой", "свои", "наши",
    "также", "либо", "нельзя", "нужно", "дай", "кстати", "вроде",
    # English
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was",
    "were", "be", "been", "being", "for", "on", "at", "by", "with", "as",
    "that", "this", "these", "those", "it", "its", "from", "not", "but",
    "if", "then", "so", "than", "too", "very", "can", "could", "will",
    "would", "should", "may", "might", "must", "do", "does", "did", "has",
    "have", "had", "we", "you", "they", "he", "she", "i", "me", "my",
    "our", "your", "their", "his", "her", "them", "us", "there", "here",
    "what", "which", "who", "whom", "when", "where", "why", "how", "all",
)

# Stop-words are compared in stemmed form, so an inflected function word
# ('которого', 'такие') is dropped as well.
STOPWORDS = frozenset(_STOPWORDS_RAW)
STOP_STEMS = frozenset(stem_token(word) for word in _STOPWORDS_RAW)


def tokenize(text: str) -> list:
    """Return lowercase Unicode word tokens of *text*.

    Punctuation and underscores are separators; ``ё`` is folded to ``е`` so
    queries like ``еще`` also match ``ещё`` in the documents. This is the
    RAW tokenizer: no stemming and no stop-word filtering (see
    :func:`content_terms` for the analysis used by the index).
    """
    normalized = str(text or "").lower().replace("ё", "е")
    return TOKEN_RE.findall(normalized)


def content_terms(text: str) -> list:
    """Return the index terms of *text* (stemmed, stop-words removed).

    Repeated terms are kept (the caller needs the frequencies); use
    :func:`_unique_terms` for a query.
    """
    out = []
    for token in tokenize(text):
        stem = stem_token(token)
        if not stem or stem in STOP_STEMS:
            continue
        out.append(stem)
    return out


def _unique_terms(text: str) -> list:
    """Return the distinct index terms of *text*, order preserved.

    The analysis is EXACTLY the one used at indexing time, so a query and a
    document are always reduced the same way. A query made only of
    stop-words therefore yields no terms and honestly finds nothing - a
    fallback to raw tokens would search for words the index does not store.
    """
    terms: list = []
    seen: set = set()
    for stem in content_terms(text):
        if stem not in seen:
            seen.add(stem)
            terms.append(stem)
    return terms


def field_terms(text: str) -> dict:
    """Return the analysed index terms of one chunk, grouped by field.

    The result always carries both fields:

      * ``body`` - terms of the chunk text with its header removed;
      * ``meta`` - terms of the header values (Документ/Продукт/Заголовок/
        Раздел); emptiness means the chunk carries no header.
    """
    body, meta = split_metadata(text)
    return {
        FIELD_BODY: content_terms(body),
        FIELD_META: content_terms(_meta_text(meta)),
    }


def indexed_terms(text: str) -> list:
    """Return every index term of a chunk (body first, then meta).

    Convenience view for diagnostics/tests; the index itself stores the two
    fields separately (see :func:`field_terms`).
    """
    terms_by_field = field_terms(text)
    out = []
    for field in _FIELD_ORDER:
        out.extend(terms_by_field.get(field, []))
    return out


# ─── low-level helpers ───────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled.

    Kept local to this module so it stays standalone; mirrors the connection
    settings of core.rag_index (ON DELETE CASCADE support).
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_has_column(conn: sqlite3.Connection, table: str,
                      column: str) -> bool:
    """True when *table* exists AND carries *column* (works on any row factory)."""
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception:
        return False
    for row in rows:
        try:
            if str(row[1]) == column:
                return True
        except Exception:
            continue
    return False


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the fielded BM25 tables (idempotent). Owns no transaction.

    Legacy v1/v2 tables lack the ``field`` column: they are dropped and
    recreated so the fielded layout can be rebuilt cleanly. The caller owns
    the transaction.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
    )
    if not _table_has_column(conn, "bm25_terms", "field"):
        conn.execute("DROP TABLE IF EXISTS bm25_terms")
    if not _table_has_column(conn, "bm25_docs", "field"):
        conn.execute("DROP TABLE IF EXISTS bm25_docs")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bm25_docs (
               chunk_id INTEGER NOT NULL REFERENCES chunks(id)
               ON DELETE CASCADE,
               field TEXT NOT NULL,
               dl INTEGER NOT NULL,
               PRIMARY KEY (chunk_id, field)
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bm25_terms (
               term TEXT NOT NULL,
               chunk_id INTEGER NOT NULL REFERENCES chunks(id)
               ON DELETE CASCADE,
               field TEXT NOT NULL,
               tf INTEGER NOT NULL,
               PRIMARY KEY (term, chunk_id, field)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bm25_terms_chunk "
        "ON bm25_terms(chunk_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bm25_terms_term_field "
        "ON bm25_terms(term, field)"
    )
    return True


def _read_meta_int(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    """Read one integer meta value (default on missing/unreadable)."""
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return int(row["v"])
    except (TypeError, ValueError):
        return default


def _read_meta_str(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Read one string meta value (default on missing/unreadable)."""
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["v"])


def _write_meta(conn: sqlite3.Connection, key: str, value) -> None:
    """Upsert one meta value."""
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, str(value)),
    )


def _init_counter(conn: sqlite3.Connection, key: str) -> None:
    """Make sure a counter row exists before an incremental bump."""
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, '0') ON CONFLICT(k) DO NOTHING",
        (key,),
    )


def _bump_meta(conn: sqlite3.Connection, key: str, delta: int) -> None:
    """Adjust one cached counter in meta (row must exist)."""
    conn.execute(
        "UPDATE meta SET v = CAST(v AS INTEGER) + ? WHERE k = ?",
        (int(delta), key),
    )


# ─── readiness / lifecycle ───────────────────────────────────────────────────

def is_ready(db_path: str) -> bool:
    """True when the BM25 index exists AND matches the current schema.

    An index built by an older schema version reports False, so the next
    search rebuilds it once instead of serving a stale analysis.
    """
    if not os.path.exists(db_path):
        return False
    try:
        conn = _connect(db_path)
        try:
            ready = _read_meta_str(conn, _READY_KEY, "")
            version = _read_meta_str(conn, _VERSION_KEY, "")
            return ready == "1" and version == str(SCHEMA_VERSION)
        finally:
            conn.close()
    except Exception:
        return False


def _terms_to_counts(terms: list) -> dict:
    """Group an already-analysed term list into ``{term: tf}``."""
    counts: dict = {}
    for term in terms:
        counts[term] = counts.get(term, 0) + 1
    return counts


def _remove_chunk_rows(conn: sqlite3.Connection, chunk_id: int) -> bool:
    """Drop postings/doc rows of one chunk and adjust the counters.

    Positional access is used because this helper also runs on foreign
    connections (core.rag_index) whose row factory may be the default tuple.
    """
    cid = int(chunk_id)
    rows = conn.execute(
        "SELECT field, dl FROM bm25_docs WHERE chunk_id = ?", (cid,)
    ).fetchall()
    conn.execute("DELETE FROM bm25_terms WHERE chunk_id = ?", (cid,))
    conn.execute("DELETE FROM bm25_docs WHERE chunk_id = ?", (cid,))
    if not rows:
        return False
    for row in rows:
        field = str(row[0])
        dl = int(row[1] or 0)
        _init_counter(conn, _len_key(field))
        _bump_meta(conn, _len_key(field), -dl)
    _init_counter(conn, _DOCS_KEY)
    _bump_meta(conn, _DOCS_KEY, -1)
    return True


def _write_chunk_fields(conn: sqlite3.Connection, cid: int,
                        terms_by_field: dict) -> int:
    """Write postings + doc lengths for one chunk (caller owns the txn).

    Returns the number of distinct terms written across all fields.
    """
    written = 0
    for field in _FIELD_ORDER:
        terms = terms_by_field.get(field) or []
        counts = _terms_to_counts(terms)
        if not counts:
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO bm25_terms(term, chunk_id, field, tf)"
            " VALUES(?, ?, ?, ?)",
            [(t, cid, field, tf) for t, tf in counts.items()],
        )
        conn.execute(
            "INSERT OR REPLACE INTO bm25_docs(chunk_id, field, dl)"
            " VALUES(?, ?, ?)",
            (cid, field, len(terms)),
        )
        written += len(counts)
    return written


def rebuild_index(db_path: str) -> dict:
    """Rebuild the whole BM25F index from the chunks table.

    Returns ``{"ok": True, "docs", "tokens", "meta_tokens", "terms",
    "version"}`` or ``{"ok": False, "error": ...}``. Idempotent; the ready
    flag and the schema version are set only when the rebuild fully
    succeeded.
    """
    if not os.path.exists(db_path):
        return {"ok": False, "error": "index database not found"}
    try:
        conn = _connect(db_path)
        try:
            ensure_schema(conn)
            conn.execute("DELETE FROM bm25_terms")
            conn.execute("DELETE FROM bm25_docs")
            rows = conn.execute("SELECT id, text FROM chunks").fetchall()
            docs = 0
            term_rows = 0
            len_by_field = {field: 0 for field in _FIELD_ORDER}
            for row in rows:
                cid = int(row["id"])
                terms_by_field = field_terms(row["text"])
                wrote = _write_chunk_fields(conn, cid, terms_by_field)
                if wrote <= 0:
                    continue
                docs += 1
                term_rows += wrote
                for field in _FIELD_ORDER:
                    len_by_field[field] += len(terms_by_field.get(field) or [])
            _write_meta(conn, _DOCS_KEY, docs)
            for field in _FIELD_ORDER:
                _write_meta(conn, _len_key(field), len_by_field[field])
            _write_meta(conn, _VERSION_KEY, SCHEMA_VERSION)
            _write_meta(conn, _READY_KEY, 1)
            conn.commit()
            return {
                "ok": True,
                "docs": docs,
                "tokens": len_by_field.get(FIELD_BODY, 0),
                "meta_tokens": len_by_field.get(FIELD_META, 0),
                "terms": term_rows,
                "version": SCHEMA_VERSION,
            }
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ensure_ready(db_path: str) -> bool:
    """Ensure the BM25 index exists and is up to date with the schema.

    Legacy bases (no tables, or an older schema version, or legacy tables
    without the field column) get ONE full rebuild here, transparently to
    the caller.
    """
    if is_ready(db_path):
        return True
    return bool(rebuild_index(db_path).get("ok"))


def drop_index_conn(conn: sqlite3.Connection) -> None:
    """Drop the BM25 tables inside an existing connection (no commit).

    Used by core.rag_index.reset_index; the caller owns the transaction.
    """
    conn.execute("DROP TABLE IF EXISTS bm25_terms")
    conn.execute("DROP TABLE IF EXISTS bm25_docs")
    conn.execute(
        "DELETE FROM meta WHERE k IN (?, ?, ?)",
        (_READY_KEY, _VERSION_KEY, _DOCS_KEY),
    )
    conn.execute(
        "DELETE FROM meta WHERE k LIKE 'bm25_len_%' OR k = 'bm25_total_len'"
    )


def drop_index(db_path: str) -> None:
    """Drop the BM25 tables and cached state of a base (never raises)."""
    if not os.path.exists(db_path):
        return
    try:
        conn = _connect(db_path)
        try:
            drop_index_conn(conn)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ─── incremental maintenance (called from core.rag_index) ────────────────────


def index_chunk(conn: sqlite3.Connection, chunk_id: int, text: str) -> int:
    """(Re)index one chunk inside an open connection. Returns term count.

    The caller commits. An existing entry for the same chunk id is replaced
    (the old doc lengths are subtracted from the cached counters).
    """
    cid = int(chunk_id)
    _remove_chunk_rows(conn, cid)
    terms_by_field = field_terms(text)
    written = _write_chunk_fields(conn, cid, terms_by_field)
    if written <= 0:
        return 0
    for field in _FIELD_ORDER:
        dl = len(terms_by_field.get(field) or [])
        if not dl:
            continue
        _init_counter(conn, _len_key(field))
        _bump_meta(conn, _len_key(field), dl)
    _init_counter(conn, _DOCS_KEY)
    _bump_meta(conn, _DOCS_KEY, 1)
    return written


def on_chunk_added(db_path: str, chunk_id: int, text: str) -> bool:
    """Keep the BM25 index in sync after a chunk write (never raises).

    No-op when the base was never BM25-indexed yet (legacy base) or when
    its index carries an older schema version: the first search performs
    one full rebuild instead of thousands of tiny writes.
    """
    try:
        if not is_ready(db_path):
            return False
        conn = _connect(db_path)
        try:
            ensure_schema(conn)
            index_chunk(conn, chunk_id, text)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def on_chunk_updated(db_path: str, chunk_id: int, text: str) -> bool:
    """Re-index a chunk whose text changed (same upsert as on_chunk_added)."""
    return on_chunk_added(db_path, chunk_id, text)


def remove_chunk_conn(conn: sqlite3.Connection, chunk_id: int) -> bool:
    """Drop BM25 postings of a chunk inside an OPEN connection (never raises).

    Same-connection variant of :func:`on_chunk_deleting` for callers that
    already hold a transaction on the index database (core.rag_index).
    """
    try:
        return _remove_chunk_rows(conn, chunk_id)
    except Exception:
        return False


def on_chunk_deleting(db_path: str, chunk_id: int) -> bool:
    """Sync counters BEFORE a chunk row is deleted (never raises).

    After this call the FK cascade in the chunk DELETE removes any rows that
    might remain, so no stale postings survive.
    """
    try:
        if not is_ready(db_path):
            return False
        conn = _connect(db_path)
        try:
            _remove_chunk_rows(conn, chunk_id)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


# ─── search ──────────────────────────────────────────────────────────────────

def search(db_path: str, query: str, top_k: int = 5, min_score: float = 0.0,
           k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> list:
    """Run an own-BM25F search over the base index.

    Returns hits sorted by descending score with the same shape as the
    vector search (``chunk_id``, ``text``, ``source``, ``chunk_index``,
    ``score``). Empty list for a missing database, blank query or an empty
    index. Each field (body/meta) contributes its own IDF-weighted BM25
    score; multi-term queries apply the coordination factor described in the
    module docstring, single-term queries keep the plain score.
    """
    if not os.path.exists(db_path):
        return []
    terms = _unique_terms(query)
    if not terms:
        return []
    if not ensure_ready(db_path):
        return []
    try:
        conn = _connect(db_path)
        try:
            n_docs = _read_meta_int(conn, _DOCS_KEY, 0)
            if n_docs <= 0:
                return []
            avgdl = {}
            for field in _FIELD_ORDER:
                total = _read_meta_int(conn, _len_key(field), 0)
                avgdl[field] = (total / n_docs) if n_docs else 0.0
            scores: dict = {}
            matched_pairs: set = set()
            for field in _FIELD_ORDER:
                weight = float(FIELD_WEIGHTS.get(field, 1.0))
                a = avgdl.get(field) or 0.0
                for term in terms:
                    rows = conn.execute(
                        "SELECT t.chunk_id AS cid, t.tf AS tf,"
                        " COALESCE(d.dl, 0) AS dl"
                        " FROM bm25_terms t"
                        " LEFT JOIN bm25_docs d"
                        " ON d.chunk_id = t.chunk_id AND d.field = t.field"
                        " WHERE t.term = ? AND t.field = ?",
                        (term, field),
                    ).fetchall()
                    df = len(rows)
                    if not df:
                        continue
                    idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                    for row in rows:
                        tf = int(row["tf"] or 0)
                        if tf <= 0:
                            continue
                        dl = int(row["dl"] or 0)
                        denom = tf + k1 * (
                            1.0 - b + b * (dl / a if a else 0.0)
                        )
                        if denom <= 0:
                            continue
                        cid = int(row["cid"])
                        scores[cid] = scores.get(cid, 0.0) + (
                            weight * idf * tf * (k1 + 1.0) / denom
                        )
                        matched_pairs.add((cid, term))
            if not scores:
                return []
            # Coordination (multi-term only): a chunk matching all meaningful
            # terms must outrank a chunk matching just one of them.
            if len(terms) > 1:
                matched: dict = {}
                for cid, _term in matched_pairs:
                    matched[cid] = matched.get(cid, 0) + 1
                for cid, score in scores.items():
                    share = matched.get(cid, 0) / len(terms)
                    scores[cid] = score * (COORD_FLOOR + (1.0 - COORD_FLOOR) * share)
            try:
                limit = max(1, int(top_k))
            except (TypeError, ValueError):
                limit = 5
            # heapq.nlargest keeps the top-k selection O(N log k) instead of
            # a full O(N log N) sort, which matters on frequent terms over
            # 20k+ chunks. The key reproduces the old tie-break exactly:
            # higher score first, then the smaller chunk id.
            ranked = heapq.nlargest(
                limit, scores.items(), key=lambda kv: (kv[1], -kv[0]))
            ids = [cid for cid, _ in ranked]
            placeholders = ",".join("?" for _ in ids)
            chunk_rows = conn.execute(
                "SELECT id, text, source, chunk_index FROM chunks"
                f" WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
            by_id = {int(r["id"]): r for r in chunk_rows}
        finally:
            conn.close()
    except Exception:
        return []

    hits = []
    try:
        floor = float(min_score or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    for cid, score in ranked:
        if floor and score < floor:
            continue
        row = by_id.get(cid)
        if row is None:
            continue
        hits.append({
            "chunk_id": cid,
            "text": row["text"],
            "source": row["source"] or "",
            "chunk_index": int(row["chunk_index"] or 0),
            "score": float(score),
        })
    return hits


def stats(db_path: str) -> dict:
    """Return diagnostic BM25 stats (sizes only; safe on missing files)."""
    out = {"ready": False, "docs": 0, "tokens": 0, "meta_tokens": 0,
           "posting_rows": 0, "version": 0, "current_version": SCHEMA_VERSION}
    if not os.path.exists(db_path):
        return out
    try:
        conn = _connect(db_path)
        try:
            out["ready"] = is_ready(db_path)
            out["docs"] = _read_meta_int(conn, _DOCS_KEY, 0)
            out["tokens"] = _read_meta_int(conn, _len_key(FIELD_BODY), 0)
            out["meta_tokens"] = _read_meta_int(conn, _len_key(FIELD_META), 0)
            out["version"] = _read_meta_int(conn, _VERSION_KEY, 0)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM bm25_terms"
                ).fetchone()
                out["posting_rows"] = int(row["n"])
            except Exception:
                out["posting_rows"] = 0
        finally:
            conn.close()
    except Exception:
        pass
    return out
# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: MIT
