# Job Intel Hub — technical, security, UX and design review

Reviewed commit `6b1a2fc`, September 5–6, 2026. Scope: database, API, worker, collectors, classification/matching/prep, scheduler/configuration, tests, and the complete frontend template.

**Assessment:** the split-pane triage model and small offline backend are appropriate, but the workstation is not yet dependable for rapid, high-volume triage. Fix stored HTML injection and mutation/selection consistency first. The existing suite passes while missing the failure sequences most likely to damage operator trust.

**Evidence:** 93 tests passed in 1.51s, with the worker lock redirected to a temporary path. Additional isolated Python and JavaScript probes reproduced connection lifetime, WAL contention, missing-profile, malformed-value, parser-inheritance, unsafe-link, and rollback defects. A real browser against a temporary database containing 55 synthetic jobs confirmed the undo/inspector mismatch and Escape inconsistency; the desktop appearance was inspected at 1280×720. Production jobs and source code were not modified. Live external scraping, dependency vulnerability scanning, screen-reader testing, mobile rendering, and measured CLS/latency profiling were not performed. Performance and responsive-layout concerns below are source-derived unless explicitly marked reproduced.

## 1. ARCHITECTURAL, CONCURRENCY & DATA INTEGRITY

### S1 — Ingestion connections are not explicitly closed

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `db.py:118`, `db.py:134`, `db.py:167`; connection setup at `db.py:16`.
- **Issue / Critique:** `with sqlite3.Connection` commits or rolls back; it does not close. Every insert opens another connection whose release depends on garbage collection. The API dependency correctly uses `finally: conn.close()`, but the worker helpers do not. A probe successfully executed SQL on the connection after leaving its context. This is nondeterministic resource retention, not evidence of an indefinitely growing leak under every Python runtime. PRAGMA setup failure also leaves the newly opened handle without explicit cleanup. Short-lived implicit cursors are generally consumed promptly; no separate persistent cursor leak was found.
- **Concrete Refactor:** use explicit closing around transaction scopes in all three helpers; close on initialization failure. Keep connections owned by one request or worker operation.

```python
from contextlib import closing

with closing(get_connection()) as conn, conn:
    conn.execute(...)  # commit/rollback first, close second

# Inside get_connection(), after sqlite3.connect(...):
try:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000").close()
    conn.execute("PRAGMA foreign_keys=ON").close()
    return conn
except BaseException:
    conn.close()
    raise
```

Enable and verify persistent WAL mode in controlled database initialization rather than negotiating it on every row insert. Explicitly close a cursor when retaining it beyond a single expression, especially around migrations.

### S2 — WAL timeout is treated as a guarantee of successful writes

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `db.py:9`, `db.py:169`, `app.py:192`, `app.py:211`, `scout.py:119`, `fetch_github.py:230`.
- **Issue / Critique:** WAL permits concurrent readers but still has one writer. A writer held beyond 15 seconds causes an unhandled `OperationalError`; API mutations fail and ingestion can abandon the remaining batch. Reproduced with a temporary WAL database and a shortened busy timeout. Current ingestion commits per row, so the comment describing “long bulk writes” is inaccurate; normal overlap alone is not proof of failure. [SQLite explicitly documents SQLITE_BUSY in WAL mode](https://www.sqlite.org/wal.html).
- **Concrete Refactor:** add a bounded retry wrapper for whole database transactions, filtering by `(exc.sqlite_errorcode & 0xff) == sqlite3.SQLITE_BUSY`; roll back and reopen before retrying, use exponential jitter, and cap the total wait. Return HTTP 503 with `Retry-After` after exhaustion and retain a retryable UI operation. Do not retry arbitrary SQL errors or replay the network collector to retry a database write. Short transactions of a bounded number of validated rows reduce connection churn. Commit status/prep writes before returning success so the receipt represents durable state.

### S3 — Missing profile crashes first requests; cache reload is non-atomic

- **Domain:** Systems
- **Severity / Impact:** High
- **Location:** `app.py:18`, `sync_profile.py:275`, `matcher.py:50`.
- **Issue / Critique:** on a fresh checkout without ignored `profile.json`, `mtime` is `None` and `_PROFILE_CACHE.get('mtime')` is also `None`; initialization is skipped and `['data']` raises `KeyError`. Reproduced. On reload, one thread clears the dictionary while another can read it. The profile writer truncates the live file, exposing partial JSON to active requests. This defeats the matcher's intended missing-file fallback.
- **Concrete Refactor:** construct a complete immutable snapshot and publish under a thread lock; check for cache existence independently from mtime. Preserve a valid last-known snapshot on a malformed edit and surface a configuration warning. Write JSON to a temporary sibling file, flush/fsync, then `os.replace` it onto the destination.

```python
from threading import Lock
_PROFILE_LOCK = Lock()
_PROFILE_SNAPSHOT = None

def cached_profile():
    global _PROFILE_SNAPSHOT
    with _PROFILE_LOCK:
        try:
            stamp = PROFILE_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if _PROFILE_SNAPSHOT is None or _PROFILE_SNAPSHOT[0] != stamp:
            candidate = load_profile(PROFILE_PATH)
            _PROFILE_SNAPSHOT = (stamp, candidate)
        return _PROFILE_SNAPSHOT[1]
```

Add schema validation and last-known-good handling around `candidate` before publication; the minimal snippet fixes missing-file initialization and the clear/update race.

### S4 — Application timestamps are overwritten by retries and cannot survive undo

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `app.py:192`, `templates/index.html:380`, `templates/index.html:410`.
- **Issue / Critique:** posting `applied` twice stamps a new time twice. Archiving an old application and undoing it makes the old application appear recent. The undo record stores only status, so it cannot restore the prior timestamp. Concurrent windows also silently overwrite each other's decisions.
- **Concrete Refactor:** add a monotonically increasing `version` and compare-and-swap updates (`WHERE id=? AND version=?`, return 409 on mismatch). Record transition ID, prior status, and prior `applied_at` in a server-side action history. Undo that transition only if it is still current. At minimum make repeat application updates idempotent:

```sql
applied_at = CASE
  WHEN ? = 'applied' AND status = 'applied' THEN applied_at
  WHEN ? = 'applied' THEN CURRENT_TIMESTAMP
  ELSE NULL
END
```

Use the history record to restore the timestamp on undo; idempotence alone does not solve historical restoration.

### S5 — Semantic deduplication silently merges distinct requisitions

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `db.py:138`, `db.py:163`, `db.py:189`.
- **Issue / Critique:** company/title/location identifies a label, not a requisition. Different teams or hiring cycles with identical labels collapse into one row; location aliases create the opposite problem. Existing duplicates never refresh descriptions, URLs or closed state. Every `IntegrityError`, including invalid track/domain, is reported as a duplicate. Deterministic hashing itself is working; semantic identity is the problem.
- **Concrete Refactor:** store `(source, source_job_id)` as a unique source identity with canonical posting URL as a fallback. Keep a separate normalized fingerprint for cross-source merge candidates. Upsert source metadata while preserving operator status and application history. Use `ON CONFLICT(source, source_job_id) DO UPDATE ...`; log/quarantine other integrity violations as invalid data. Normalize Unicode and location aliases explicitly, and encode tuple components with JSON rather than ambiguous `|` concatenation if retaining a fingerprint.

### S6 — Startup migrations inspect schema outside the migration transaction

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `db.py:67`, `db.py:83`, `db.py:112`, `worker.py:63`, `app.py:45`.
- **Issue / Critique:** app and worker both migrate on startup. The applied-at existence check can run twice before either process performs `ALTER TABLE`, leaving the second process with a duplicate-column error. The status rebuild checks before its write lock, allowing redundant rebuilding. The rebuild also drops unrecognized legacy columns/indexes or fails copying columns absent from the new schema; current tests cover only the expected schema.
- **Concrete Refactor:** acquire `BEGIN IMMEDIATE` before reading a schema version, apply ordered migrations under that lock, set `PRAGMA user_version`, and commit once. Remove inner commits/BEGINs from migration helpers. Copy an explicit supported column list and deliberately recreate indexes/triggers. Add a simultaneous-startup test and a rollback-on-copy-failure test.

**Lock guard assessment:** `worker.py:54–80` uses kernel `flock`, not an existence/PID-token check. It is atomic on the supported POSIX workstation, and close/process death releases ownership; an empty file left after a crash is not an orphaned lock. Do not “fix” this by unlinking the lock during release, which can split lock ownership across inodes. Opening with `a+` avoids unnecessary truncation. Catch only EAGAIN/EACCES as contention, re-raise other lock errors, and fail explicitly on unsupported platforms if mutual exclusion is required. Direct collector entry points bypass the worker lock, so document them as diagnostic commands or route scheduled ingestion through the worker. No claim of a stale PID-token race is warranted here.

## 2. PIPELINE RESILIENCE & SECURITY HYGIENE

### P1 — Stored external URLs can inject HTML event handlers

- **Domain:** Security
- **Severity / Impact:** High
- **Location:** `templates/index.html:114`, `templates/index.html:311`, `db.py:159`, `scout.py:72`.
- **Issue / Critique:** `safeUrl()` verifies only a prefix and returns raw text that is inserted into `href="${url}"`. A stored URL such as `https://example.com/" onclick="alert(1)` produces a new `onclick` attribute. The actual renderer was exercised in an isolated JavaScript harness and emitted that attribute. A malicious source value can execute script in the workstation origin when the posting is clicked, read jobs and mutate statuses. This is a link-attribute vulnerability; escaped description text is not the vulnerable sink.
- **Concrete Refactor:** parse URLs, allow only HTTP(S), and set DOM properties instead of concatenating attributes. If retaining the template, use `href="${esc(url)}"` after URL validation. Backend validation must reject invalid/control-character URLs too.

```javascript
function safeUrl(value) {
  try {
    const u = new URL(String(value));
    return ['https:', 'http:'].includes(u.protocol) ? u.href : null;
  } catch { return null; }
}
// Prefer a DOM-created anchor:
const link = document.createElement('a');
link.href = safeUrl(d.url);
link.textContent = 'View posting';
link.target = '_blank';
link.rel = 'noopener noreferrer';
```

Only create the anchor when validation succeeds. Also remove inline handlers containing unescaped IDs (`index.html:325`, `329`, `357`), use `data-id` properties and delegated listeners, and validate backend IDs. Current collectors generate safe hashes, but `insert_job` accepts arbitrary caller-provided IDs.

### P2 — Frontend execution depends on unpinned external assets

- **Domain:** Security
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:7`, `requirements.txt:7`.
- **Issue / Critique:** the page executes a remote Tailwind script with access to the local application's origin; fonts use `@latest`. This adds a supply-chain trust boundary, availability dependence and changing layout. Most Python top-level packages are pinned, but `pypdf>=4.0` and transitive dependencies are not locked. No known-CVE claim is made; an advisory scan was not run.
- **Concrete Refactor:** build and serve versioned CSS and font assets locally, lock Python transitives with hashes, and run dependency auditing in CI. After moving JavaScript and inline handlers into local assets, add a response CSP such as `default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`. Remove inline styles or provide an explicit policy for them before enabling enforcement. Preserve loopback binding; any shared deployment needs authentication and authorization. For local browser-origin hardening, allowlist Host values and validate Origin for mutating browser requests.

### P3 — Rate limiting produces repeated attempts and false-success run summaries

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `worker.py:35`, `worker.py:77`, `worker.py:114`, `worker.py:154`, `fetch_github.py:212`, `scout.py:110`, `scout.py:124`.
- **Issue / Critique:** the worker discards collector return values. GitHub returns source failure outcomes, but the worker reports only new IDs; complete source failure still exits zero and can notify “No new roles.” Scout continues the remaining queries after a board block, using fixed jitter rather than failure-aware backoff. No application-level total deadline contains a hung scraper. JobSpy may have internal timeout/retry behavior, but the repository does not enforce or report it.
- **Concrete Refactor:** make each source return structured `ok/partial/blocked/failed` outcomes with fetched/inserted/rejected counts. Persist them and propagate failure to CLI exit status. Honor `Retry-After`, retry transient network/5xx failures with a bounded jittered budget, and open a per-board circuit on repeated 429/403 or challenge responses. Stop that board for the run; do not attempt to bypass challenges. Run collectors with an enforceable process deadline and cleanup. A 200 response with zero recognizable rows should be “format unrecognized” unless a known empty-source marker validates it.

### P4 — One malformed row can abort a batch; missing numeric values become real data

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `scout.py:47`, `scout.py:60`, `scout.py:74`, `scout.py:88`, `db.py:179`.
- **Issue / Critique:** `float('nan')` is truthy: a missing URL becomes `'nan'`, a missing remote flag becomes `True`, and location becomes `'nan'`. Reproduced. Pandas nullable values can also raise during truth testing, aborting the rest of the frame because error isolation is outside `_ingest`. Raw HTML descriptions remain raw strings, reducing readability when safely escaped and causing inconsistent normalization across sources.
- **Concrete Refactor:** convert Pandas missing scalars to `None` before string/bool operations; validate each payload through a schema with HTTP URL, finite values, explicit nullable boolean, bounded text lengths, valid enums and normalized dates. Catch validation failures per row and increment `rejected`, not `duplicates`. Store a normalized plain-text description using an HTML parser that removes script/style nodes and preserves paragraph/list breaks; optionally retain raw source content separately. Continue HTML escaping at rendering. If rich descriptions are later supported, use an allowlist sanitizer at that boundary rather than the current regex cleaner.

### P5 — Closed company rows corrupt following sub-listing attribution

- **Domain:** Systems
- **Severity / Impact:** High
- **Location:** `fetch_github.py:135`.
- **Issue / Critique:** a closed row is skipped before updating `last_company`. When Acme has an open row, Globex has a closed first row, and Globex's next role uses `↳`, that next role is inserted under Acme. Reproduced with a three-row fixture. This changes company identity, deduplication and application decisions.
- **Concrete Refactor:** update company context from every explicit company cell before applying closed/role/domain filters. Then resolve sub-listing company from that context and skip the closed job itself. Reset inheritance at table/section boundaries; do not inherit across unrelated tables. Add the closed-first-row fixture to parser tests.

### P6 — Track detection confuses substrings with employment evidence

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `classifier.py:32`, `classifier.py:33`, `scout.py:62`, `fetch_github.py:170`.
- **Issue / Critique:** `Internal Software Engineer` is classified as an internship because it contains `intern`; reproduced. Internships without an explicit season receive the obsolete factual-looking “Summer 2026 / General” label. A source's 2027 hint is ignored once internship classification succeeds. Query fallback also promotes uncertainty into a confirmed employment track.
- **Concrete Refactor:** use token boundaries such as `r'\b(?:intern(?:ship)?s?|co[- ]?op)\b'`; parse explicit season/year, otherwise use a source term hint or `None`. Store `track_basis` and confidence (`explicit_title`, `description`, `source_hint`, `query_hint`) and expose inferred classifications. Test Internal/International titles and an undated intern role from a 2027 source.

**SQL and description assessment:** request filter values, job IDs and statuses are parameterized; dynamic SQL fragments are internally constructed, not user-supplied identifiers. No direct request-driven SQL injection was found. `%` and `_` remain LIKE wildcards, which is search semantics rather than injection; escape them if literal search is intended. `esc()` protects rendered company/title/description/prep strings in their present text contexts. Raw-description storage is not itself execution; the unsafe link sink in P1 is the actionable XSS finding. The notification subprocess uses an argument vector and escaped strings, not a shell command.

## 3. USER EXPERIENCE (UX), WORKFLOW & COGNITIVE LOAD

### U1 — A failed mutation undoes a different job

- **Domain:** UX & Workflow
- **Severity / Impact:** High
- **Location:** `templates/index.html:383`, `templates/index.html:399`, `templates/index.html:406`.
- **Issue / Critique:** save A, dismiss B, then let A's request fail. A's catch calls global `undo()`, popping B. The JavaScript probe showed the third request targeting B, not A. If the network is still down, rollback also fails because it requires another network request, leaving the supposedly reverted UI wrong. A timeout may also represent a successful server commit whose response was lost.
- **Concrete Refactor:** assign a unique operation ID; snapshot `{id, before, after, viewKey, index}` immutably; make the failure callback reconcile that operation only. Roll back local state without needing a second POST when rejection is definitive. For ambiguous transport errors, mark “Sync uncertain” and query authoritative state. Serialize mutation/undo per job and use the server version/action history from S4. Remove only the corresponding failed undo entry; never pop unrelated global history from an error callback.

### U2 — Undo leaves stream selection and inspector describing different jobs

- **Domain:** Ergonomics
- **Severity / Impact:** High
- **Location:** `templates/index.html:418`, `templates/index.html:421`.
- **Issue / Critique:** undo inserts an item and re-renders rows without updating cursor identity or inspector. In the browser, `s`, then `z` restored Company 2 at the selected index while the inspector still showed Company 4. Clicking “Mark Applied” and pressing `a` can now target different jobs. Undo can also insert an item from a previous filter context without rechecking current filters.
- **Concrete Refactor:** store `selectedId` as the source of truth, derive its index, and after rollback either restore the undone job as selection or preserve the currently selected ID. Reconcile rows against the active view/filter key and call `selectRow()` on the resulting index so detail and highlight agree. Never mutate list structure and stop at `renderRows()`.

### U3 — Out-of-order list responses overwrite current filters

- **Domain:** UX & Workflow
- **Severity / Impact:** High
- **Location:** `templates/index.html:236`, `templates/index.html:426`, `templates/index.html:433`.
- **Issue / Critique:** debouncing search does not order responses. An older request can replace a newer result while tabs/search continue displaying newer filters. During reload, old rows remain actionable under the new status; `setStatus` uses the new view state to decide removal. `keepCursor` preserves an index rather than identity, and the boot skeleton remains on initial failure.
- **Concrete Refactor:** use an AbortController plus generation token. Publish rows only when both generation and view key still match; preserve selected ID where appropriate. Disable triage against stale rows while the new view is pending. Render a retryable error state rather than an indefinite skeleton.

```javascript
let listGeneration = 0;
let listController;
async function fetchCurrentView(params) {
  const generation = ++listGeneration;
  listController?.abort();
  listController = new AbortController();
  const jobs = await api('/api/jobs?' + params, {
    signal: listController.signal,
  });
  if (generation !== listGeneration) return;
  // Reconcile selection by ID, then atomically publish this view and rows.
  return jobs;
}
```

Handle AbortError silently; display actual failures persistently and bind retry to the failed view.

### U4 — Undo visibility and synchronization receipts do not match their promises

- **Domain:** UX & Workflow
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:150`, `templates/index.html:391`, `templates/index.html:405`, `templates/index.html:142`.
- **Issue / Critique:** only the toast expires after three seconds; the stack does not. Long after the affordance disappears, `z` can reverse old actions, including after changing views. Failed undo pops the action before success and loses retry history. “Marked saved” omits job identity and claims completion before synchronization. `loadStats()` runs before the POST and is not called on successful acknowledgment, so badges can lag indefinitely. Errors disappear after four seconds and unrelated request success clears them.
- **Concrete Refactor:** choose and document a coherent policy: persistent undo history with an accessible History/Undo control is preferable for fast triage; the toast may be transient. If a strict three-second window is required, store `expiresAt`, enforce it in both keyboard/button paths, and retain recovery through history. Keep undo entries pending until confirmed. Use job-specific receipts (“Saved · Acme — Engineer · syncing”), then confirm on acknowledgment; refresh counters after commit. Keep actionable errors with Retry until reconciled. Add `role="status" aria-live="polite"` to receipts and `role="alert"` to mutation errors.

### U5 — “Best match” hides all but the newest 500 jobs

- **Domain:** UX & Workflow
- **Severity / Impact:** Medium
- **Location:** `app.py:96`, `app.py:109`, `templates/index.html:218`.
- **Issue / Critique:** SQL limits by discovery date before Python scores. A high-match older job cannot appear in Best match, and the interface displays “500 results” without a truncation warning despite larger tab counts. The displayed ordering is only best within a hidden time window. Same-second discovery ties have no explicit ID tie-break.
- **Concrete Refactor:** persist scores keyed to profile version and sort/filter before cursor pagination, using `(score DESC, date_discovered DESC, id ASC)`. Recompute scores when the profile changes. Return `{items,total,next_cursor}` and show “500 of 1,240” with Load more. For a small local dataset, computing all filtered scores before pagination is a simpler initial fix than introducing a background score table.

### U6 — Match receipts overstate the evidence and prioritize irrelevant gaps

- **Domain:** UX & Workflow
- **Severity / Impact:** Medium
- **Location:** `matcher.py:28`, `app.py:131`, `templates/index.html:291`, `templates/index.html:323`.
- **Issue / Critique:** the score examines only title/track/domain, yet the UI says “Match fit” and “Not mentioned in this role.” In the synthetic browser fixture, the description explicitly mentioned Python and distributed systems, but description evidence does not contribute. All absent profile terms become “missing,” alphabetically capped at six; important and irrelevant terms get equal visual priority. A 45% title overlap is not a calibrated probability of fit.
- **Concrete Refactor:** immediately label the metric “Title/category overlap” and gaps “Profile terms absent from title/category,” show “Description not evaluated,” and remove `%` probability framing. Rank receipts by weight, not alphabetically. For richer matching, generate normalized description terms once at ingestion and score against those in both list/detail, with evidence snippets and field provenance. Separate absent profile terms from actual job requirements the candidate lacks; do not infer the latter from absence alone.

### U7 — Prep shows invented accomplishments and discards useful prompts

- **Domain:** UX & Workflow
- **Severity / Impact:** Medium
- **Location:** `prep.py:96`, `prep.py:102`, `app.py:211`, `templates/index.html:365`.
- **Issue / Critique:** “Delivered…with measurable impact” and “production-ready work” are generic generated assertions with no resume evidence. The UI labels them “Key alignment,” encouraging mistaken trust. The more actionable `talking_points` are returned but never rendered or persisted, so disappear on detail reload.
- **Concrete Refactor:** label the current provider “Preparation prompts · rule-based”; replace accomplishment claims with fill-in prompts such as “Choose a project demonstrating API design; add your measured result.” Persist/render `talking_points` as a checklist. Only generate declarative resume bullets when tied to a specific verified profile project and show that evidence. Add copy/edit affordances and a regenerate state; keep generic prompts visibly separate from verified alignment.

### U8 — Empty views and pipeline liveness collapse distinct system states

- **Domain:** UX & Workflow
- **Severity / Impact:** Medium
- **Location:** `app.py:179`, `templates/index.html:178`, `templates/index.html:198`, `templates/index.html:247`.
- **Issue / Critique:** newest discovery is labeled “Scraped,” although a successful zero-new run does not change it. No running/completed/failed state is persisted or polled. “No positions in this view” cannot distinguish first setup, inbox complete or unavailable sources. Clear filters exists, but initial seed and failure recovery lead to dead ends. Background discoveries do not appear automatically.
- **Concrete Refactor:** persist ingestion run start/end/status, heartbeat, source failures and counts separately from job discovery. Poll this small endpoint while visible. Use distinct messages: “No jobs collected yet” with the seed command; “Inbox cleared” with Saved/Applied links; “No matches” with Clear filters; “Collecting…” with last completion; “Collection failed” with source diagnostics/retry guidance. Offer “N new roles available” without reordering the active triage cursor automatically.

## 4. INTERACTION ERGONOMICS & WORKSTATION FEEL

### E1 — Global shortcuts collide with focus, repetition and native activation

- **Domain:** Ergonomics
- **Severity / Impact:** High
- **Location:** `templates/index.html:439`.
- **Issue / Critique:** input/textarea/select guards are helpful, but buttons/links, contenteditable and IME composition are not guarded. Enter opens the selected posting while native button activation can also occur. Holding `x` repeatedly dismisses successive rows. Escape clears only the inspector; browser testing confirmed the row remains selected and shortcuts still act on it. `j/k` cannot return focus to a focusable stream because none exists. Global single-character shortcuts have no off/remap setting.
- **Concrete Refactor:** make the stream focusable and scope triage keys to it; allow `/` as a configurable global accelerator. Ignore composition and repeated destructive/prep keys, and prevent default for handled keys. Preserve native Enter on controls. Escape should return focus to the stream without claiming selection is empty, or actually clear `selectedId` and disable triage. Search Escape should return focus to the stream. Add ArrowUp/ArrowDown/Home/End alternatives and a discoverable shortcut-help/settings control. [WCAG 2.2 requires a mechanism to disable/remap character shortcuts or activate them only when the relevant component has focus](https://www.w3.org/TR/WCAG22/#character-key-shortcuts).

```javascript
if (e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return;
if (e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
if (e.target.closest('button, a')) return;
if (e.repeat && ['a', 's', 'x', 'z', 'p'].includes(e.key)) return;
// After matching a key within the focused stream:
e.preventDefault();
```

Integrate search Escape before the editable guard and explicitly focus the stream; this snippet is the guard portion, not a replacement for the full dispatcher.

### E2 — Inspector can show actionable stale content and prep steals selection

- **Domain:** Ergonomics
- **Severity / Impact:** High
- **Location:** `templates/index.html:277`, `templates/index.html:360`, `templates/index.html:368`.
- **Issue / Critique:** selecting an uncached job leaves the previous inspector and its active buttons visible until fetch finishes, or indefinitely on failure. The existing detail-ID guard correctly rejects ordinary stale responses, but prep completion calls `loadDetail(oldId)`, resetting that guard and replacing the inspector after the user has moved on. `event?.target` uses an implicit browser global; clicking the nested `<kbd>` can disable/relabel that child instead of the button, and keyboard invocation targets the focused element. Pending detail responses can also repopulate a cache after invalidation.
- **Concrete Refactor:** key detail requests/cache by job ID and data version; show a stable loading shell with disabled actions immediately when selection changes. Use an explicit button argument (`e.currentTarget`) and a `prepPending` set keyed by ID. On prep completion update that job's cache but render only if `selectedId === id`; do not call a function that changes selection. Preserve or restore focus deliberately after rendering and show a non-disruptive completion receipt for offscreen prep.

### E3 — Scroll and layout behavior break continuity during long sessions

- **Domain:** Ergonomics
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:242`, `templates/index.html:245`, `templates/index.html:273`, `templates/index.html:300`, `templates/index.html:344`, `templates/index.html:390`.
- **Issue / Critique:** reload resets cursor to zero but calls selection with scrolling suppressed, so a previously scrolled stream can leave its active row offscreen. Optimistic deletion also suppresses visibility correction. Replacing the whole inspector leaves scroll position subject to prior content height and clamping, and variable title/chip/prep blocks move the action row. Nested description scrolling adds a second scroll boundary. The split-pane container itself is stable; no measured Core Web Vitals CLS regression is asserted.
- **Concrete Refactor:** retain selection by ID on refresh and ensure the selected row is visible inside `#stream` after every structural change. For an intentional new filter, reset stream scroll to zero. Reset detail scroll on a different job; preserve it for same-job updates. Move decision buttons into a persistent sticky action bar, reserve header/loading space, and use one inspector scroll region with an explicit description disclosure. Update only changed row nodes/counters rather than rebuilding up to 500 rows for every mutation. Validate with a recorded 50-role keyboard session and measured input-to-paint timings.

### E4 — Visual controls lack complete accessible state and names

- **Domain:** Ergonomics
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:48`, `templates/index.html:54`, `templates/index.html:64`, `templates/index.html:86`, `templates/index.html:210`, `templates/index.html:325`.
- **Issue / Critique:** the browser accessibility tree reports every tab unselected, including the visually active Inbox. Listbox has no tabindex or active-descendant linkage. Filter buttons do not expose pressed state, sort's label lacks `for`, and missing-term disclosure is a clickable span. Toast/error state is not announced. Keyboard help omits p/Enter/Escape and disappears below the large breakpoint.
- **Concrete Refactor:** implement tab `aria-selected`, roving tabindex, keyboard arrow behavior and controlled panel IDs; add `aria-pressed` to filters. Use `label for="sort"` and a persistent accessible search label. Give options stable DOM IDs and the focusable stream `aria-activedescendant`. Replace disclosure spans with buttons exposing `aria-expanded`/`aria-controls`. Provide visible `focus-visible:outline-2 focus-visible:outline-sky-400 focus-visible:outline-offset-2` treatment and a help button at all widths. Use the live-region changes from U4.

## 5. VISUAL HIERARCHY, DESIGN TOKENS & AESTHETIC POLISH

### V1 — Muted text fails normal-text contrast

- **Domain:** Visual Polish
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:39`, `templates/index.html:65`, `templates/index.html:324`, `templates/index.html:356`.
- **Issue / Critique:** zinc-500 (`#71717a`) on zinc-900 (`#18181b`) is **3.67:1**; on zinc-950 (`#09090b`) it is **4.12:1**. Small chip text, placeholders and shortcut descriptions need 4.5:1. Actual header muted text was confirmed as `rgb(113,113,122)` in the browser. Zinc-400 on zinc-900 is **6.91:1**. Alpha backgrounds require compositing for exact ratios, but the opaque endpoints already fail for zinc-500. Decorative dividers need not meet text contrast; essential control boundaries require separate non-text contrast evaluation. [WCAG contrast minimum](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
- **Concrete Refactor:** replace information-bearing `text-zinc-500` and `placeholder-zinc-500` with `text-zinc-400` / `placeholder-zinc-400`. Remove opacity reduction from small tab counts or select a token passing after compositing. Keep borders subtle where grouping remains clear, while ensuring focus/selection indicators remain explicit. Update README's blanket “WCAG-compliant” claim until keyboard, contrast and state semantics are verified.

### V2 — Stream spends scarce width on repeated categories instead of decision evidence

- **Domain:** Visual Polish
- **Severity / Impact:** Low
- **Location:** `templates/index.html:209`, `templates/index.html:212`, `templates/index.html:214`, `templates/index.html:319`.
- **Issue / Critique:** the 44px targets and fixed inspector support a good scan rhythm, but repeated track/domain badges consume row width while company truncates at 128px. At 1280px, even the synthetic company names truncate. Location/remote eligibility require selecting every row, while score is a hover-only dot and most sub-75 results share “Fair.” Compensation is absent from schema and normalization, so there is no salary formatting to audit. Match chips occupy more prominent space than eligibility or direct actions.
- **Concrete Refactor:** keep 44px row height; use a grid with a numeric score, flexible company/title and a concise location/remote field. Hide the redundant category column when its filter is fixed. Offer full names on focus as well as hover. Prioritize company → role → eligibility → evidence → action in the inspector. Add nullable compensation minimum/maximum/currency/period from source data before displaying it; format with `Intl.NumberFormat`, retain hourly versus annual units, and show “Compensation not provided” instead of inventing a range. Use 12px metadata and 14px primary text where widths permit, maintaining roughly 1.4–1.5 line-height outside the compact stream.

### V3 — Fixed-width desktop structure cannot reflow at narrow widths or zoom

- **Domain:** Visual Polish
- **Severity / Impact:** Medium
- **Location:** `templates/index.html:31`, `templates/index.html:61`, `templates/index.html:86`, `templates/index.html:328`.
- **Issue / Critique:** the 340px minimum stream, 45% split, nonwrapping toolbar and four-button action row have no compact breakpoint. At small widths or high zoom, available inspector width collapses and overflow can be clipped by the body. This is a source-derived responsive risk, not a completed mobile conformance test.
- **Concrete Refactor:** apply `min-w-0` to the inspector, `flex-wrap` to toolbars/action groups, and switch below a measured breakpoint to a single-pane list with an explicit “Back to results” detail view. Restore selection/scroll/focus when returning. On desktop, use a bounded resizable split rather than a fixed percentage. Validate at 320 CSS pixels and 200%/400% zoom, including all filters and action buttons.

### V4 — Date labels conflate discovery with posting and mishandle timestamp forms

- **Domain:** Visual Polish
- **Severity / Impact:** Low
- **Location:** `templates/index.html:116`, `templates/index.html:208`, `templates/index.html:342`.
- **Issue / Critique:** a discovery fallback is still labeled “posted.” `parseTs` appends Z to any string without T, including date-only values, relying on nonuniform parsing; ISO strings with T but no timezone use local time although SQLite timestamps are UTC. Rounded relative units can show 24h before the day boundary, and future dates are clamped into “1m.”
- **Concrete Refactor:** normalize API timestamps to ISO 8601 with explicit UTC offsets, keep posting calendar dates distinct, and parse only documented formats. Label “Discovered” when no posting date exists. Use `Intl.RelativeTimeFormat`, floor interval boundaries, preserve future-date direction, and include an absolute date in a `<time datetime="…" title="…">` element. Unknown/invalid dates should display a deliberate fallback.

### V5 — Style literals make hierarchy hard to maintain consistently

- **Domain:** Visual Polish
- **Severity / Impact:** Polish
- **Location:** `templates/index.html:14`, `templates/index.html:20`, `templates/index.html:191`, `templates/index.html:229`, `templates/index.html:315`.
- **Issue / Critique:** CSS hex values, alpha surfaces, Tailwind class strings and duplicated button/chip definitions express the same hierarchy independently. Font-medium repetitions and separate kbd styling obscure the intended token system. The overall restrained dark palette is suitable; maintainability and state consistency need work more than decorative redesign.
- **Concrete Refactor:** define semantic tokens for canvas/surface/raised surface, primary/secondary text, control border, focus and selection, plus row height and spacing. Derive CSS and reusable class helpers from those tokens. Centralize action variants and all default/hover/focus/disabled/pending states. Keep the existing reduced-motion media query. Package local font assets with matching declared family names and fallbacks to stabilize first render.

## 6. VALIDATION COVERAGE & IMPLEMENTATION ORDER

### T1 — Passing tests do not exercise the state-machine boundaries

- **Domain:** Systems
- **Severity / Impact:** Medium
- **Location:** `test_pipeline.py:183`, `test_pipeline.py:260`, `test_pipeline.py:532`, `test_scheduler.py:173`, `pytest.ini:1`.
- **Issue / Critique:** all 93 tests pass, but coverage is primarily pure functions and sequential API calls. There are no frontend interaction/race tests, no real concurrent-startup migration test, no busy-exhaustion response test, and no stored-link injection regression. The lock test covers an already-held lock, not process death/release. Global warning filters do not establish lifecycle correctness.
- **Concrete Refactor:** add behavior-driven regression tests for: A fails after B succeeds; undo before A acknowledgment; ambiguous mutation timeout; undo across filter changes; selected ID matching inspector after undo; reversed list/detail completion order; prep completion after navigation; missing/corrupt profile reload; malformed Pandas values; closed-company inheritance; distinct requisitions with identical labels; repeated apply timestamps; lock release on killed child; and simultaneous migration processes. Add browser tests for search focus, Enter on buttons, held destructive keys, offscreen selection after filtering and narrow reflow. Assert rendered links cannot acquire injected attributes. Keep all database/lock fixtures temporary and mock board calls/notifications.

**Suggested order:**

1. Close the stored-link injection boundary (P1), fix profile startup (S3), and correct wrong-job mutation/undo/selection paths (U1–U3, E1–E2).
2. Repair company attribution (P5), lifecycle/transaction handling (S1–S2, S4, S6), and ingestion normalization/outcomes (P3–P4, P6).
3. Make receipts, history, pagination and evidence truthful (U4–U8); complete keyboard/accessible state (E3–E4).
4. Apply contrast, responsive reflow, row evidence and semantic tokens (V1–V5), then validate a 50-role triage session under latency and failures.

A timed user study is still needed to substantiate “50+ roles in minutes without fatigue.” The browser session establishes functional inconsistencies, not a measured cognitive-load score or a throughput benchmark.
