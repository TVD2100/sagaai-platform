# RAG Base Creator

## Purpose
This skill explains how to create and populate RAG knowledge bases in SagaAI
using the platform's own Python API (`core.rag`, `core.rag_index`,
`core.rag_embeddings`, `core.rag_chunker`).

Knowledge bases are NOT created from the settings UI. The settings "Storage"
page is read-only: it only lists bases, shows their status/stats, runs test
semantic search, and allows browsing/editing/deleting individual chunks.
Creating and populating bases is YOUR job as the orchestrator, and this skill
gives you the exact workflow plus a ready-to-run helper script.

## When to use
Use this skill when the user asks to:
- create a RAG knowledge base from a folder of documents or from a list of files;
- re-index or extend an existing base with new content;
- delete a base or clean up chunks programmatically;
- build a base for a specific assistant or employee (rag_slots).

## Prerequisites
1. The user selected the SagaAI install as the workspace (or you know the
   install root from the `current_install()` tool). You need the install root
   so `core.*` modules are importable: the install root is the folder that
   contains `core/`.
2. Embedding credentials: RAG bases use the YandexAI service for embeddings
   (IAM token + Folder ID configured in LLM settings). Verify them FIRST:
   ```python
   from core.rag_embeddings import get_yandex_embedding_credentials
   api_key, folder_id = get_yandex_embedding_credentials()  # raises if missing
   ```
   If credentials are missing, STOP and tell the user to configure YandexAI
   API keys in the LLM settings. Offer a dry-run plan instead (see below).

## Before generation: volume assessment and approval (MANDATORY)
Do NOT start building a base immediately. Before ANY real run (and before
`--no-embed` runs that insert chunks) you MUST:

1. **Ask for the exact source.** The user must explicitly tell you which
   folder or file list to index. Never invent or guess the source path.
   If the user did not specify one, ask for it and do not proceed without it.
2. **Build a plan first.** Walk the source and compute an honest volume
   summary:
   - number of files that will be indexed (after exclusions and extension
     filter);
   - total size in characters (and approximate MB);
   - estimated number of chunks (one chunk per file when it fits;
     split larger files; use `build_chunks()` or `--dry-run` to get real
     numbers);
   - estimated number of tokens (approximate: 1 token ≈ 4 ASCII chars or
     ≈ 2 Cyrillic chars; or use `core.files.estimate_tokens`);
   - expected embedding time: roughly 500-2000 chunks per minute depending
     on network/latency. Give an honest estimate and clearly mark it as an
     estimate.
   Use `--dry-run` to produce the exact file/chunk counts where possible.
3. **Check the embedding model availability BEFORE starting.** Determine
   which provider and which embedding model will be used (by default
   YandexAI / `text-search-doc` (документные эмбеддинги для индексации RAG; legacy-алиас `embeddings` маппится на неё); см. `core.rag_embeddings.get_yandex_embedding_credentials`
   and the platform's service config). Report this to the user in the plan.
   If the provider/model cannot be verified as available (no credentials,
   wrong model id, unknown provider), STOP and tell the user what is missing.
   Do not start generation in that case.
4. **Get explicit approval.** Present the plan above (source, volume summary,
   provider/model, expected time) and WAIT for the user's explicit approval
   before running the build. Do not auto-start.
5. **Cost: do NOT proactively report cost estimates.** Cost is affected by
   many factors and can easily be wrong, so by default do not mention
   monetary cost at all. ONLY if the user explicitly asks for a cost estimate:
   provide a rough upper-bound estimate, clearly labeled as approximate and
   non-guaranteed, and recommend a small `--limit N --dry-run` trial to
   calibrate it.

## Chunking rules (important)
Follow these rules unless the user explicitly requests something else:

1. **One file = one chunk when it fits.** If the file contents (plus its
   context header) are SHORTER than or equal to `chunk_size` (default 1800
   characters), store the whole file as a single chunk. Do not split small
   files.
2. **Large files are split into SELF-DESCRIBING parts.** If the file is larger
   than `chunk_size`, split it at paragraph/sentence boundaries into parts of
   about `chunk_size` minus the header. Prefix EVERY chunk (including
   single-chunk files) with a context header so a reader immediately knows
   what document the piece belongs to, which product section it covers, and
   which part of the whole file it is:

   ```
   Документ: docs/ai-studio/api-ref/authentication_in_yandex_ai_studio_api.md
   Продукт: AI Studio
   Заголовок: Authentication in Yandex AI Studio API
   Раздел: <H2 → H3 trail, when the file was split>
   Фрагмент: 1/1 - документ целиком   |   Фрагмент: 2/5
   ```

   - `Документ` - relative path from the source root.
   - `Продукт` - human-readable product name derived from the `docs/<product>/`
     path component; for files outside `docs/` falls back to `YaAgentAI`.
   - `Заголовок` - the first H1 heading of the file (fallback: file name).
   - `Раздел` - the nearest H2 → H3 trail active at the part's position; only
     present for files that were split into multiple chunks.
   - `Фрагмент` - 1-based part number and total (`N/M`), or the marker
     `1/1 - документ целиком` for whole files.
3. **Part numbering:** parts are 1-based (`1`, `2`, `3` ...) and each part
   knows the total (`N/M`), so summaries can reference exact ranges.
4. **Default chunk size is 1800 characters.** Yandex text-search embedding
   models accept at most ~1024 tokens per request (roughly 2000 Russian
   characters); 1800 leaves a safe margin. Only change it when the user
   explicitly asks.
5. **Exclusions:** by default `index.md` files are excluded from traversal.
   The user may pass additional exclude patterns.
6. Do not index binary or non-text files; the default extension list is
   `.md` and `.txt`. Add more explicitly only when asked (e.g. `.py`, `.csv`).

## Quick path: use the bundled helper script
A ready-to-run helper lives next to this file:
`scripts/build_base.py`. It implements the rules above and the full pipeline.

Get its absolute path from the skill folder (the folder containing this
SKILL.md), then run it via `run_code` with `path=<absolute path to
scripts/build_base.py>` and command-line arguments:

```
python <skill_folder>/scripts/build_base.py --install-root <INSTALL_ROOT> \
    --source <SOURCE_DIR_OR_FILE> --name "My KB" [--slug my_kb] \
    [--exclude index.md] [--chunk-size 1800] [--chunk-overlap 0] [--limit N]
```

Arguments:
- `--install-root`: the SagaAI install root (from `current_install()`).
- `--source`: a directory (scanned recursively) or a single file or several
  `--source` values.
- `--name`, `--description`: base display name/description.
- `--slug`: optional stable slug (derived from the name when omitted).
- `--chunk-size` / `--chunk-overlap`: chunking parameters (default
  `--chunk-size 1800`, `--chunk-overlap 0`).
- `--exclude`: file name(s) to skip (repeatable; default `index.md`).
- `--extensions`: extra file extensions to include (repeatable; the defaults
  are `.md` and `.txt`).
- `--limit N`: process only the first N matched files (useful for trials).
- `--slots`: one or more assistant/orchestrator identifiers allowed to use
  the base (default: empty = everyone; pass `dev_agent` to restrict it).
- `--replace`: replace an existing base with the same slug (never use it
  without the user's explicit confirmation).
- `--dry-run`: only print the collection/chunking plan, create nothing.
- `--no-embed`: create the base and chunks but skip embeddings (base stays
  in `draft` status). Use only for a user-approved trial.

The script prints a JSON result on stdout (base slug, status, chunk counts,
warnings) and exits 0 on success, 1 on error.

## Manual workflow (when the script is not suitable)
1. Follow the mandatory "Before generation" approval flow first.
2. Verify embedding credentials (see Prerequisites).
3. Collect files (recursive walk; exclude `index.md` and any user-specified
   exclusions; text files only).
4. For each file build chunks per the Chunking rules above. The bundled
   `build_chunks(text, rel_path, chunk_size, chunk_overlap)` helper in
   `scripts/build_base.py` implements the full header logic (Документ /
   Продукт / Заголовок / Раздел / Фрагмент) - reuse it whenever possible
   rather than reimplementing the header/trail logic by hand.
5. Create the base:
   ```python
   from core import rag
   base = rag.create_base(name="...", description="...", provider="YandexAI",
                          embedding_model="text-search-doc", chunk_size=1800,
                          chunk_overlap=0, rag_slots=[...])
   slug = base["slug"]
   db = rag.index_db_path(slug)
   ```
6. Embed and insert every chunk with the correct `source` and `chunk_index`:
   ```python
   from core.rag_embeddings import embed_text
   from core.rag_index import add_chunk
   vector = embed_text(text, model=base["embedding_model"])
   add_chunk(db, text, source=relative_path, chunk_index=i, vector=vector)
   ```
7. Mark the base ready:
   ```python
   rag.set_status(slug, "ready")
   ```
8. Verify with a test query:
   ```python
   from core.rag_search import search_base
   hits = search_base(slug, "your test query", top_k=3)
   ```
   Report the top hits (source + score) to the user.

## Assigning the base to employees (rag_slots)
- At creation time pass `rag_slots=[...]` (assistant ids / orchestrator slugs).
- For employees afterwards:
  ```python
  from core.orchestrators import set_orchestrator_rag_bases
  set_orchestrator_rag_bases("<employee slug>", ["<base slug>", ...])
  ```
- `rag_slots=[]` means the base is available to everyone.

## Uploaded files: two modes (IMPORTANT)
When the user provides documents via the chat uploader (attached files)
rather than as a path on disk, follow this rule:

1. **Small files / short instructions** (typically ≤ ~30-60 KB of extracted
   text, or when the user says "use this as context/instructions"): read the
   full text and pass it in the chat context, as usual.
2. **Large uploaded files** (the ones meant to become a RAG base, or files
   too big to paste into context): save them into the dialogue's file folder
   first:
   ````
   <history_dir>/<thread_id>/files/<filename>
   ````
   Where `<history_dir>` is `core.paths.HISTORY_DIR` and `<thread_id>` is the
   current devagent/orchestrator thread id. Then pass to the build workflow:
   - the on-disk absolute path of the saved file (preferred: point
     `--source` at the saved file/folder directly), or
   - if you must continue from memory, pass its metadata only
     (name, path, size, first ~2 KB as a preview) and read the full file when
     needed for chunking.
3. Never embed the full text of a large file into the chat context just to
   build a base - that defeats the purpose of the RAG pipeline. Use `--source`
   on the saved path.

## Error handling
- Missing YandexAI credentials -> explain how to add them (LLM settings) and
  offer a `--dry-run` plan.
- A base with the same slug already exists -> choose another slug or ask the
  user whether the old base should be replaced (never silently overwrite).
- Embedding request fails midway -> keep the already inserted chunks, report
  exactly how many were added, and suggest re-running with a `--resume` style
  pass or checking the quota/network.
- The settings Storage page is read-only: never instruct the user to create
  or populate bases from that page.

## Example task (bundled scenario)
User: "create a base from Yandex AI Studio docs at
`<install>/docs/YaAgentAI/docs`, pick all files except index.md, split files
that exceed the chunk size into self-contained parts, one chunk per file
when it fits, and build the base."

Expected execution:
1. Verify YandexAI credentials.
2. Tell the user the selected embedding provider/model (YandexAI /
   `text-search-doc`) and confirm the explicit source path from their message.
3. Walk `<install>/docs/YaAgentAI/docs` recursively, keep `.md`/`.txt`,
   drop every `index.md`.
4. Build a `--dry-run` plan first and show the volume summary (files, size,
   estimated chunks/tokens, expected time). Ask for explicit approval, or
   start with a small `--limit 5 --dry-run` / `--limit 5 --no-embed` trial
   for calibration.
5. Files <= chunk_size become single chunks with the self-describing header
   (`Фрагмент: 1/1 - документ целиком`).
6. Larger files are split with the self-describing header block
   (Документ/Продукт/Заголовок/Раздел/Фрагмент).
7. Create the base, embed + insert all chunks, set status `ready`, run a test
   search.
8. Report: base slug, chunk count, sample hits. Do not report monetary cost
   unless the user asked for it.
