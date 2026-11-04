# core/version.py - single source of truth for the SagaAI platform version.
#
# The platform is published as a stable release: the 2.x line keeps its
# public APIs, UI and data formats backward compatible; breaking changes
# would arrive in a new major version. The version follows SemVer.
#
# 2.0.0 changes the distribution terms: since this release the platform is
# licensed under PolyForm Noncommercial 1.0.0 (commercial use requires a
# separate agreement). Versions 1.x and earlier remain under the MIT License.
#
# 2.1.0 adds the chat folder selector ("Выбор папки"): a static block with
# "Авто", up to 10 recent workspaces and a native OS folder dialog; the picked
# folder is applied automatically on the next send, and the DevAgent prompt
# (v3.21) offers the numbered recent-folders menu for a concrete project.
#
# 2.1.1 restores chat feed auto-scroll (core.feed_autoscroll): while the agent
# works the feed follows the newest output, a manual scroll-up pauses the
# follow and returning to the bottom resumes it.
#
# 2.1.2 removes the module's own bottom padding: Streamlit 1.66 already
# reserves room for the pinned bottom bar, so the manual padding-bottom gave a
# double inset and left a large empty gap between the last message and the
# input form.
#
# 2.1.3 moves file upload into the chat input itself: the bulky separate
# file_uploader dropzone is replaced by st.chat_input(accept_file="multiple")
# (paperclip with a tooltip); files travel with the text submission, and a
# files-only submission uses the i18n placeholder naming the attachments.

# 2.1.4 adds the cold-start dependency audit (core.dependency_check): on
# the first start the platform checks installed libraries and their versions
# and warns the user about missing or outdated packages (with a pip hint);
# requirements.txt now pins streamlit>=1.66.0 instead of an exact ==.
#
# 2.1.5 makes save confirmations visible: a message painted right before
# st.rerun() was discarded with the current pass, so settings pages never
# showed "Saved". Announcements now go through ui.components.flash (a queue in
# session_state) and are painted by render_flash() on the next pass.

#
# 2.1.6 keeps a settings save in place: the employee settings page tracks
# the open section, so after st.rerun() the user stays on the tab they were
# editing instead of landing on the first one, and the queued confirmation
# is visible next to that section (a repeated save is confirmed every time).
# A failed save now reports an error instead of silently doing nothing, and
# a failing folder mirror no longer discards a successful database write.

# 2.2.0 adds the connector-creation architecture through DevAgent: draft
# connections (credentials_status=pending) are created in the chat without
# any secret, filled on the Connectors page, then verified and enabled for
# orchestrators; every connection keeps an agent-maintained memory notes
# file. The connector_creator instruction, the ssh/github instruction
# updates and the credentials-pending marker in the prompt block complete
# the flow.

#
# 2.3.0 adds MCP support through connectors: a from-scratch JSON-RPC 2.0
# stdio client (core/mcp_client.py, stdlib only, no official SDK) plus the
# mcp connector service (command whitelist, env_json secret). Orchestrators
# get mcp_list_tools / mcp_call_tool and dynamic mcp__<conn>__<tool> tools;
# untrusted server data is sanitized, non-text results are rejected and a
# hung server is terminated without blocking the session. The mcp_connector
# DevAgent instruction documents the flow.

#
# 2.4.0 extends MCP through connectors with a preset system: bundled MCP
# server templates (defaults/connectors/mcp/, no secrets) that DevAgent
# instantiates as pending drafts - the user later fills only the credential
# fields on the Connectors page (friendly secret_env_fields mapped to
# env_pairs). MCP connections gain an http transport (https-only or
# http://localhost endpoints) with SSE responses, Mcp-Session-Id replay and
# an Authorization header template resolved from the decrypted connection
# secrets per request; secret updates drop the cached client so the new
# credentials reach the next call. Presets ship for Vkusvill (http),
# GitHub MCP (http) and the official Everything demo (stdio); the
# Connectors page offers a preset picker plus an advanced mode, and the
# mcp_connector instruction documents the preset flow and http transport.

# 2.5.0 reworks the Connectors page MCP UX: MCP servers get their own
# tab with icon preset tiles (a click instantly instantiates the preset
# as a pending draft) and a "+ custom server" tile with a stdio/http
# form. The preset dropdown is gone, the Everything demo preset is no
# longer shipped, and the MCP connections list lives inside the tab.

# 2.6.0 reworks the whole Connectors page around three tabs and one-click
# flows. Tabs keep a stable key and an explicit pin before every rerun, so
# the selected tab survives any click or save; a new "Connections" tab
# lists ALL saved connections (GitHub, SSH, MCP) as cards with the service
# icon, credential state, test / edit / delete actions. General services
# became icon tiles: a click selects the service AND opens the create form
# at once (no second click on the expander), and a successful create lands
# on the Connections tab. An MCP preset tile creates the connection and
# immediately opens its credential form on the Connections tab, so the
# "draft" is never invisible; the same applies to the custom stdio/http
# server form. MCP HTTP errors 401/403 now carry an actionable hint: the
# GitHub Copilot endpoint rejects classic ghp_* PATs and needs a
# fine-grained github_pat_* token.

# 2.7.0 reworks the Connectors page into a list <-> form model, the same
# pattern the assistants page uses: a page-level view key
# (connectors_view = list | create | edit) decides what renders, and a
# form REPLACES the list instead of opening inside an expander or on
# another tab. Clicking a service tile, an MCP preset tile or the custom
# server tile now only opens the create form - nothing is written. The
# connection is persisted solely by the Create button and is active from
# the start (the typed credentials are encrypted with it), so the page
# never leaves an invisible credential-less draft behind; Cancel and the
# back link return to the list without writing anything, and a failed
# save keeps the form open with the entered values. The edit form follows
# the same full-page pattern, and pending drafts created from the chat are
# completed through it with a "Fill credentials" button. Tabs became
# stateful: st.tabs(on_change="rerun", key=..., default=<label>) - without
# on_change="rerun" the widget keeps no state at all, so the stored tab was
# ignored and every rerun fell back to the first tab. After a save the list
# reopens on the Connections tab through the default label.
#
# instantiate_preset gained account/secrets/pending: the page passes
# pending=False and submits the credentials together, while the default
# pending=True keeps the agent's draft-first flow unchanged.

#
# 2.7.1 hides the GitHub MCP preset from the MCP servers tab: the remote
# server at https://api.githubcopilot.com/mcp/ accepts only fine-grained
# PATs carrying the account permission Copilot Requests and answers
# 403 access denied to anything else, so the tile sent users into a dead
# end. Presets gained an optional "hidden" flag: a hidden preset leaves
# the tile catalogue (list_presets) but stays resolvable by id, so a
# connection created from it earlier keeps its preset form and clearing
# the flag brings the tile back. GitHub work goes through the github_rest
# connection (ghr_* tools); any third-party MCP server is still added via
# the "+ Custom MCP server" tile.

# 2.8.0 adds the optional url-fetch model to employee settings: the
# employee can be given a page-fetch model, and only providers that declare
# a fetch_models catalog are offered (DeepSeek ships one with
# deepseek-flash and deepseek-v4-pro over its Search/Fetch API at
# https://api.deepseek.com/v1/tools/fetch). The page is downloaded by
# core.url_fetch (POST {"url"}) and its Markdown body is sanitized and
# labelled [DATA_FROM_URL_FETCH] as untrusted data, capped at 30,000
# characters. While the model is assigned, the tool appears in the
# employee's tool catalog, its '## Available tools' prompt block and the
# system-functions list; clearing the model - or the provider dropping it
# from the catalog - removes the tool again.

# 2.9.0 renames that tool to url_fetch (page_fetch stays a legacy alias)
# and widens what it can fetch. Two new sources join the provider API: a
# PDF link is downloaded directly and its text extracted page by page
# (pdfplumber with a pypdf fallback), and a 'Use Selenium' checkbox lets
# the tool render any page in a headless browser and hand the visible text
# to the employee's main model for Markdown conversion. Direct downloads
# pass an SSRF guard that rejects loopback, private, link-local and
# service-discovery hosts. url_fetch is offered when a fetch model is
# assigned OR Selenium is enabled, and disappears once both are gone.

# 2.9.1 refreshes DevAgent's shipped defaults and tidies two settings
# behaviours. The employee now starts on DeepSeek/deepseek-flash for the
# main model with the xhigh reasoning effort, for image analysis and for
# web search (temperature 0.4, three tool calls, the high effort); image
# generation stays unassigned, so a fresh install needs no extra setup.
# The Models tab draws the Selenium checkbox first and hides the
# page-fetch model selectors while it is on, keeping the stored pair for
# when it is cleared; save confirmations are now painted below the page
# body instead of above the page title.

# 2.10.0 adds multi-agent orchestration: an employee can spawn another
# employee (spawn_agent) and call an assistant once (invoke_assistant)
# through the core/orchestration package - a versioned run contract,
# an orchestration service with a run tree, a slot pool, six delegation
# limits (depth, active children, steps, total runs, timeout, token
# budget), cascade cancellation and fan-out of independent spawns in the
# executing phase. Children work in the parent's folder with their own
# derived thread and task journal; the Delegation settings tab hands out
# the allowlists and limits.

# 2.11.0 makes documents readable without code: core/document_text
# extracts text from PDF, DOCX, PPTX, XLSX, XLS, RTF, ODT, HTML and plain
# text, the new read_document tool opens a file on disk by absolute path
# with a line window, and read_thread_file returns attachment text the same
# way; run_code is marked as a last resort. Delegation gets explicit
# contexts: spawn_agent carries the absolute paths of the parent workspace
# and its dialog files folder, invoke_assistant reads a document as one
# call, and the system prompt lists the available agents and assistants
# with their ids.

# 2.12.0 makes delegation asynchronous and resumable. A child can be
# launched with background=true: the dialog gets its run_id at once, keeps
# working, and learns about the finish from the tree mailbox delivered as a
# hidden system notice on the next step, never as a visible user line. Runs
# are stored durably (atomic records, heartbeat, heal of a dead claim by
# stale heartbeat / foreign boot id / missing pid) and observed through
# subagent_status (one run, a whole tree, or a bounded wait),
# read_subagent_state (the child's own journal by absolute path) and
# resume_subagent, which continues from the journal: finished steps and the
# files they produced are not redone, the run keeps its tree and counters,
# and a resume chain is capped. A child has no human to confirm anything, so
# a call that needs confirmation is refused with confirmation_required
# instead of hanging the run.

# 2.13.0 makes the prompt-injection protection mode follow a delegated child.
# A child used to start protected on its own, and a launch that explicitly
# asked for less could be lost on the way to the runner - the dialog's Safety
# setting applied to the parent and not to the child. The mode is now resolved
# once at spawn (an explicit request, else the mode of the run being resumed,
# else protected) and frozen in the run record, so a background child never
# reads the setting of a dialog that may already be gone. A one-step viewing
# permission approved for a single withheld payload is not a mode and is not
# inherited, and the model cannot change any of this from a tool argument.

# 2.14.0 splits memory into two stores. The personal layer
# (core/personal_memory.py) keeps notes about HOW to work - an owner card
# shared by every employee plus per-employee and per-project scopes, capped
# at 64 KiB, written atomically with revision headers and ten recoverable
# versions, screened for credentials, and archived instead of deleted when an
# employee leaves; it lives in DATA_DIR/personal and never leaves the
# installation. The working-data folder (MEMORY_DIR, resolve_data_dir) keeps
# the role's own registries, cards and journals inside the working project
# and travels with it. Three agent tools read and write the personal layer,
# the owner card and a size-only employee block are injected into the system
# prompt, the employee settings page gets a Memory tab, and the
# employee_creator instruction gains a data-structure profile section that
# picks FLOW / CASE / COURSE / ARTICLE / RESEARCH layouts by the shape of the
# unit of work.

# 2.14.1 makes the memory rules reach every employee from the first dialog.
# The `## Memory` block used to appear only once an employee already had
# notes, so a fresh install never taught the practice: the one place that
# could start it stayed silent. The block is now emitted always, and it
# states WHEN to write - after the owner corrects an approach, after a
# decision that will matter next time, after a discovered limit - instead of
# only how. The owner profile is shared, so it can be extended with append
# but not rewritten wholesale: a replace needs overwrite_owner_profile, set
# only on an explicit owner request. The catalog no longer claims `user` as
# the default read scope (it resolves to the employee's own scope), and a
# role whose working-data folder is gone is told to ask rather than silently
# start a new one.

# 2.15.0 makes subagent failures diagnosable. A raise inside the child loop
# is no longer reported as a provider failure: it becomes internal_error with
# retryable=False, while a provider failure keeps provider_error and stays
# retryable, so a caller can tell "retry once" from "this will fail the same
# way". The run record freezes the child thread id and the workspace before
# the child starts, and the finish appends a Runtime outcome paragraph to the
# child journal even when the child never wrote one. The delegation tools
# carry a note field that turns a status into the next decision - platform
# defect, retryable provider, exhausted step budget, expired wait meaning
# still running, empty journal meaning an unwritten record. A state
# assignment in the child_mode branch of the loop is fixed.

# 2.16.0 adds the blocking sleep tool. Instead of polling subagent_status in
# idle steps, a parent waits for an event or a timeout: sleep(seconds=...,
# until="any_child"|"timeout") blocks a step, and the caller picks the
# duration - a child is a full agent, not a command, and may work for tens of
# minutes. The ceiling MAX_SLEEP_SECONDS = 3600 (one hour) only guards against
# an accidental frozen step and marks a shortened wait as clamped. The wait is
# a poll with a growing interval - the
# orchestration core has no condition variable - and each tick checks the
# cancellation of the caller, the terminal runs of the tree and the mailbox
# read-only: sleep never drains a notification. The answer carries woken_by
# (event / timeout / cancelled) and finished, the run ids that became
# terminal during the wait, never their summaries or artifacts - those are
# read with subagent_status or read_subagent_state. A timeout cancels
# nothing, since ending a wait is not ending the work. The tool is enabled
# for children too, and the tree root (the mailbox and run-list key) is
# derived from the parent record because a child knows only its parent.
#
# 2.16.1 synchronizes the wait and the work budgets. The default time budget
# of one child run rises from 600 s to an hour, since sleep is built around a
# full agent rather than a command, and an upper bound
# MAX_SUBAGENT_TIMEOUT_SECONDS clamps a hand-written value - a child must not
# outlive the dialog that spawned it. MAX_SLEEP_SECONDS is derived from that
# same constant instead of being a second, drifting number, and a blocking
# spawn waits for a pool slot no longer than min(run budget,
# DEFAULT_POOL_WAIT_SECONDS), so a busy pool cannot stall one call for an
# hour. The child heartbeat is refreshed from step events at most once a
# minute: a long healthy run must not be healed into a dead one.

# 2.17.0 retunes the delegation defaults against real usage and stops task
# journals from piling up. New orchestrators start with five active
# children (was 3), 300 steps per child (was 20) and 64 runs per tree
# (was 32): the step cap now sits well above what a real task uses, so a
# long run ends on its time budget rather than on a counter that stops
# healthy work, and a fan-out of 4-7 agents no longer refuses its fourth
# child. Journal scaffolds are created before a task starts, so an install
# accumulated one file per dialog and one per delegated run, most of them
# empty forever; prune_task_state_journals now removes an empty journal
# older than a week and the journal of a finished delegated run whose
# parent journal is gone, while the live dialog, fresh files, journals
# with history and foreign files stay untouched. An empty parent does not
# cascade onto its children in the same pass, so a working child is never
# swept, and the sweep covers both the project folder and the empty-state
# dialog folders once per process.

# 2.17.1 settles two chat affordances that were easy to misuse. The
# economy-mode checkbox is gone: the compact context is the supported way
# to run a dialog, so ECONOMY_MODE_ALWAYS_ON is passed to the agent loop
# unconditionally while the loop itself still honours the flag. The
# historical chat_economy_mode config key stays readable but is no longer
# consulted, so a saved False cannot switch the mode off. The separate
# "Обзор…" button of the folder block is gone too: sitting next to the
# chat-input paperclip it read as "attach a file". The native dialog now
# opens from the last entry of the folder selector (FOLDER_PICKER_BROWSE);
# the entry is an action, not a value, so a fresh widget key after the
# dialog closes makes the selector fall back to the chosen folder whether
# the dialog returned a path or was cancelled.

# 2.18.0 adds the marketplace foundation (core.marketplace): a catalog
# client, an installer for skills with a journal, rollback and recovery of an
# interrupted run, a conflict matrix for a locally edited installation, an
# update path with per-entity refusals and a bounded run journal. The section
# lives behind one master switch (marketplace.enabled): it gates the sidebar
# entry, the one-time seeding of registry rows and the imported defaults, so
# a disabled marketplace leaves no trace in the UI and writes nothing. Every
# start-up path stays offline - the catalog is reached only from a button -
# and the RAG half of the section is not part of this slice.

# 2.19.0 extends the marketplace from skills to every shipped entity type and
# stops seeding the catalog entities from the distribution. An agent installs
# into an orchestrator folder (config merge, instructions, functions), a base
# through its manifest, source and index with an atomic folder replace, and an
# MCP server as a credential-less connection that waits for secrets. A bundle
# previews a recommended set with checkboxes, the update check reports all four
# types in their own sections and addresses an entity as type:id, and the
# installable-type list is read by the showcase instead of a trial call. The
# catalog lives in a separate GitHub repository published by
# scripts/marketplace_publish.py; entities listed as catalog-only are no longer
# imported from defaults while the marketplace is on, and a removal marker
# still wins over that rule.

# 2.19.1 fixes the false "subagents running" reports. A dialog root run stands
# for one step and has no worker that could ever write its final status, so
# release_tree closes it explicitly and a new step reopens the finished record
# instead of inheriting the previous step's status. A status reader judges
# liveness more strictly than the file repair: a stored running claim without a
# live in-memory counterpart is reported as failed/stale_run, the whole store is
# swept for records of previous processes at startup, and the heartbeat timer
# is started only for a background run, because a blocking child keeps its
# record in memory until it returns.

# 2.20.0 adds helpers to the marketplace: an ``agent`` entity of kind
# ``assistant`` installs as an assistant profile (a folder with manifest.json,
# prompt.md and text attachments, plus a row in the assistants table) next to
# the existing employee kind, which installs an orchestrator folder. The kind
# travels with the manifest, the catalog index, the showcase card, the
# registry row and the Updates row, so the showcase labels a card without
# downloading its package; a manifest without the field is read as an
# employee. The publisher refuses an unknown kind, and an installation
# refuses to switch the kind of an installed agent, because the kind decides
# both the folder and the table. Attachments are limited to text files the
# storage layer can hold, and an update keeps the attachments the user added
# while it leaves an edited prompt untouched.

# The marketplace gained helpers in 2.20.0 and, in 2.21.0, the navigation
# that makes them first-class. In 2.21.1 a card keeps its expanded state in
# the session, so its single install button covers both the entity and the
# ticked extras - and actually reaches the installer on the rerun that
# follows a click. In 2.22.0 the bundled Rag Base Creator is installed by
# default again: it is seeded into the folder named after its preset, so one
# skill exists on disk, the catalog adopts that copy instead of offering a
# second one, and a catalog installation of the same skill stops the seed.

# 2.23.0 puts the parent in charge of the children it started. A delegated
# run can be stopped with cancel_subagent: the request is a flag checked at
# step boundaries (a tool call already in flight finishes first), the run and
# its descendants end as cancelled, and only runs of the caller's own tree
# are addressable. read_subagent_thread shows what a child actually saw and
# said, not just what its journal claims. A child now reports its finish
# through a done loop status instead of a question, keeps no plan gate it
# cannot pass, and a run that ends without a summary is reported as
# unfinished work rather than as a result. The blocking wait gained
# until="all_children".

# 2.25.0 lets a parent check what a child reported. A fan-out returns
# several results in one step but never a check that they are right, so the
# new verify_result tool runs a separately appointed verifier agent against
# ONE finished run of the caller's own tree and answers a structured verdict:
# passed, failed or inconclusive. The verifier is independent because of what
# it does not see - the target's summary, artifacts, error code and counters
# reach it, the transcript and the reasoning do not, and it opens the
# artifacts itself; a verifier reading the executor's train of thought would
# agree with it instead of checking the outcome. The answer is parsed
# conservatively ("passed" next to objections is a failure; an unparsable or
# unevaluable answer is "inconclusive", never success), and a verifier that
# fails to do its work yields an inconclusive verdict rather than an error of
# the call. Checking a check is refused by both the agent and the goal marker,
# the tool is offered only while a verifier is appointed (its own right, next
# to the spawn allowlist), and the launch goes through the same service path
# so the depth, pool and total-run limits apply to a verifier as to any child.

# 2.24.0 reads the pictures a document carries, not only its text. The
# extract_document_images tool lifts embedded pictures out of PDF, DOCX,
# PPTX and XLSX, writes them as files inside the project and hands them to
# analyze_image in groups of five - the per-request limit. Pages of a PDF
# without a text layer (a scan) are rendered on request, and a rendered page
# replaces the pictures stored on that page, so the same material is never
# read twice. The analysis reply is fenced as untrusted image data.

# 2.26.0 closes the background lifecycle. A parent could start a child in
# the background and watch it, but never collect what it produced: the run
# record is bookkeeping, its summary is capped and its artifact list is not
# kept at all. The new subagent_result tool answers the second question -
# the final text, the artifacts and the resources used - in the SAME shape a
# blocking spawn returns, for a background or blocking run alike. An
# unfinished run yields no result: the answer is ready=false plus the current
# state, with no result key at all, because partial work presented as a
# result looks like success and is therefore worse than an error. The wait is
# bounded by the same 30-second ceiling as the status check. A terminal run
# record now carries its frozen result, so the answer survives the registry
# being released, and one builder (contracts.result_from_record) turns a
# record back into a result for both this tool and the verifier. Stopping a
# child already existed and was left untouched - a second stop tool with a
# different name would only make the model guess.

# 2.26.1 restores the chat feed auto-scroll itself. The snippet went through
# st.components.v1.html, which Streamlit 1.66 deprecates with a removal date
# that logs a warning on every call, and which runs inside a sandboxed iframe
# that cannot reach the app's scroll container. It is now injected with
# st.html(unsafe_allow_javascript=True) - the same main-document path the theme
# reloader and the clipboard button already use - and the container lookup
# follows the current layout (stAppScrollToBottomContainer / #stMain, since
# section.main no longer exists and the testid overrides stMain). A monotonic
# render id keeps the injected body changing so Streamlit re-runs the script on
# every rerun.

# 2.27.0 detaches employee working data from the project. The role folder is
# central by default - DATA_DIR/agents_data/<slug>/ - so one employee keeps one
# memory across projects and a workspace switch no longer hides its registries
# and journals. resolve_data_dir gained explicit scopes (workspace:<subpath>,
# central:<subpath>), reports the legacy <project>/<slug>_data folder instead of
# silently preferring it, and defaults to the central root. The working folder
# became a second root of the file tools: a path is addressed with the memory:
# prefix or an absolute path inside it, and the tools keep working before a
# project is picked, because memory is not a property of the project. The
# platform block ## Working memory declares the path once for every employee,
# and core/memory_migration.py with scripts/migrate_agent_data.py moves an old
# layout (plan only by default, source never deleted).

# 2.28.0 moves subagent conversations out of devagent.db into an append-only
# file store (dev_agent/transcript.py): one JSONL segment per child under
# <history>/<parent>/subagents/<child>/, rotated by size, compressed in place
# when cold, and stripped of UI render events that duplicated roughly half of
# every tool_result. A child no longer needs a dialog row: the derived thread
# is addressed through the threads index (transcript_path,
# parent_thread_id), so read_subagent_thread works on a real run instead of
# answering "thread not found". A database written before the file store is
# switched once by core/devagent_db_archive.py: the old file is archived into
# a verified zip beside itself, a fresh empty base is created, and a one-shot
# notice names the archive and the dialogues it preserves.

# 2.28.1 fixes the working-data migrator: the central root is called
# agents_data and therefore ends in _data, exactly like a legacy folder
# <slug>_data. An installation whose DATA_DIR is its own project root - the
# usual single-folder setup - made the planner report that root as the legacy
# folder of an employee called "agents" and offer to move every employee's
# data into agents_data/agents/. The central root is now recognised by path,
# not by its name, and a genuine legacy folder beside it is still reported.

# 2.29.0 moves the assistant dialogues out of the messages table. Every
# assistant chat is mirrored into the same append-only transcript store the
# orchestrator dialogs use (core/thread_mirror.py holds the file half shared
# by both databases), and its thread row keeps the index over the file. A
# database written before the switch is converted once at cold start by
# core/chat_db_archive.py: the whole file is archived into a verified zip
# beside itself, every dialogue is written to its transcript, the index
# columns are filled, and only then do the moved message rows leave the
# table. The database file itself is NOT replaced - assistants, settings,
# connectors and the marketplace registry live in it - so a failure at any
# step leaves the rows where they were and an un-migrated dialogue keeps
# working from the table exactly as before. The user is told once where the
# archive is and how many dialogues moved.

# 2.30.0 gives every employee a clock. The model has no clock of its own, so
# any answer about "today", "tomorrow" or "this week" was either a guess or
# the date a file happened to record when it was written - a schedule summary
# aged silently and kept answering for the previous day. A ## Current date
# block is now appended to the system prompt of every orchestrator, stating
# the day in words (weekday and month from fixed tables, not from the process
# locale) plus the installation timezone. It is the last block of the prompt
# on purpose: it is the only block that changes with time, so midnight
# invalidates the tail of the provider cache rather than the whole prefix.
# The block carries no time of day, because a minute-precision stamp would
# break the prefix cache on every step. config['current_date_enabled']
# removes the block for one employee and leaves the tool alone. The new
# current_datetime tool answers what the fact cannot - the time of day, the
# weekday of an arbitrary date, epoch - and builds its sentence with the same
# helper the prompt block uses, so the two sources cannot drift apart.

# 2.31.0 lets a subagent ask its own agent a question. A child that misses a
# fact in its brief had no way to ask: guessing cost an hour of work, and the
# only channel left was stopping the run and starting a new one through
# resume_subagent, which threw away the same run's context and budget. The
# new ask_parent tool parks the run in place: the record becomes
# awaiting_input with await_reason=question, the question, its options, the
# spent round and an absolute deadline are written next to the run, and the
# loop stops at a step boundary - same run_id, same journal, same files on
# disk. answer_child (parent only) resumes that very run with the delivered
# [PARENT ANSWER] message and refuses an expired wait; a second question comes
# back as a new parked question instead of a silent loss. Questions travel to
# the parent as fenced data, both in a blocking result and in the hidden
# background notice, because a child quotes files and web pages. Five dialog
# limits (rounds, question and answer length, answer wait, simultaneous
# waits) are frozen into the record at spawn, clamped to documented defaults
# so a missing value never disables a limit, and expire_parked finishes an
# unanswered run as failed/answer_not_received while keeping its partial
# work. The dialog survives a restart: roots_for_thread restores the tree
# from the dialog id and revive_parked_run lifts the record back into the
# registry on the first answer. The chat shows a waiting row with the agent
# and the question, and the step feed renders the question as an expandable
# block.

# 2.32.0 ships a general-purpose executor with the platform. A user who
# started from an empty installation had no employee to delegate real work
# to: the only way to get one was to find it in the marketplace, and anyone
# who never opened that page simply did without. The bundled
# Universal-Agent (defaults/orchestrators/universal_agent/) is seeded by the
# same idempotent bootstrap as the other built-ins, takes its role from the
# brief instead of a fixed specialisation, and is available to every
# employee out of the box: the right lives in config.universal_agent_enabled,
# a key whose absence reads as enabled, so a new employee is granted it
# without a migration and a user revokes it with one checkbox above the
# agent list. revoking never rewrites allowed_agents, and get_effective_agents
# is the single list the prompt advertises and the dispatcher enforces, so
# a refused call is never advertised. The same entity also ships in the
# catalog: LEGACY_DEFAULTS adopts the preinstalled copy (registry row, no
# second install), so the storefront shows an update rather than a duplicate
# and an install refreshes the same folder.

# 2.32.1 trims the optional dependency set without losing a feature. Office
# text now comes from core.ooxml (zipfile plus ElementTree), so python-docx,
# python-pptx and openpyxl are gone; pdfminer.six went with pdfplumber, whose
# PDF text path moves to pypdf and whose page rendering moves to the
# pypdfium2 build it already pulled in. tiktoken, reportlab, markdown, PyPDF2
# and xlrd leave a codebase that no longer reached them. Pillow and paramiko
# stay: the first decodes and re-encodes picture formats no header parser can
# handle, the second is the only credential-safe path to SFTP attributes.

# 2.33.0 ships Task Scout, an employee that finds the tasks nobody wrote
# down. It asks the delegated employees first (spawn_agent), reads their
# working-memory folders and, only as a last resort, their dialogs, then
# separates what is already open from what was promised and what follows
# from both. The role ships disabled by consent: the delegation list is
# empty, so a fresh installation has an agent that says whom to tick
# instead of inventing a to-do list. Two read-only helpers
# (agent_memory_reader, agent_thread_reader) enforce that same roster, so
# a guessed slug or dialog id cannot widen what the user allowed.

# 2.34.0 adds scheduled runs. A rule belongs to one employee: a task, an
# anchor moment, a step (minutes..months), a time zone kept with the rule, and
# an exclusion mask (blocked weekdays, blocked periods, quiet hours) with an
# explicit skip/postpone policy. The scheduler is an in-process thread, so it
# fires while the platform is open and dies with it; the page says so and
# shows the heartbeat instead of pretending otherwise. One point fires at
# most once: the claim is a conditional UPDATE plus a UNIQUE(schedule, planned
# point) index, so a restart or a second tick cannot repeat a run, and a
# backlog after downtime is recorded as missed rather than replayed as a
# burst. A scheduled run IS an ordinary dialog - same thread table, no parent -
# so the owner reads what an agent did last night in History like any other
# conversation, and every run row links to it. Runs are bounded by steps,
# time and an optional token budget, never delegate further, and three
# failures in a row put the rule on hold with the reason on its card.

__version__ = "2.34.0"

# SPDX-FileCopyrightText: 2026 SagaAI Platform, Deinekin T.V.
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
