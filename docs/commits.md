# Upstream commit evidence

The opt-in `commits` batch collector records historical upstream inclusion.
Git fetching and analysis run in collection; pages and APIs read saved snapshots.
Historical evidence does not establish current behavior, fix a distributed binary,
or authorize an upgrade. No upstream code or arbitrary SPEC rebuild is executed.

## Configuration and collection

Start with the [repository policies](../config/commits-distribution.example.toml)
and [package policies](../config/commits-packages.example.toml). They exercise
bounded QEMU/LLVM windows and are not loaded automatically. Distribution policy
uses `[commits]`; package policy uses `monitors.commits`. Normal catalog/override
loading, whole-policy replacement and configuration drift checks apply.

A repository requires canonical public HTTPS `url` and `web_url`, an explicit
`refs/heads/...` ref and full immutable `window_start`. `object_format` supports
sha1/sha256. The exclusive discovery boundary `window_start` and optional exclusive
target-analysis boundary `analysis_start` are separate. A branch tip discovers
candidates; the existing version decision selects a newer release for comparison.

Each package selects a configured repository and unique rules, including at least
one positive rule. Rules support `keywords`, `patterns`, `paths`, `case_sensitive`
and `exclude`. Text alternatives and path alternatives are unions; text intersects
paths within a rule. Positive rules are unioned, then matching exclusions win.
Text defaults to case insensitive. Case-sensitive repository-relative globs keep
`*` within a path segment and let `**` recurse, including hidden paths. Saved
reasons retain rule ID, field, original fragment and matching path.

Optional `mappings` are keyed by exact release version and require a full `oid`,
current SPEC `source_revision`, trusted `source_url`, public `review_url` and
positive mapping `version`. Optional `ref` must be a release tag. Current Source0
must agree with the reviewed mapping; contradictory trusted full commit evidence
cannot be overridden. Version strings, homepages and mutable tags alone do not
establish identity. The examples omit mappings pending operator review.

Use the existing CLI: `python -m tracker.collector --config CONFIG --db DB --only
commits` (on one line). The image entrypoint schedules configured policies.
`commits` is independent of the HTTP adapter runner and must not be added to
`[monitors].enabled`. No new dependency or separate scheduler is required.

## Evidence and budgets

- `original_reachable`: the original object belongs to the target history.
- `strict_patch_evidence`: a reachable single-parent text change has identical
  changed raw paths, modes and complete before/after content hashes.
- `backport_needs_review`: patch-id or an unambiguous source-object reference
  provides an association without strict equivalence.
- `no_evidence_in_range`: supported checks completed within the declared range;
  this is not a semantic absence claim.
- `unknown`: identity, missing objects/history, budgets or unsupported commit shape
  prevents a conclusion; the reason and completed range remain visible.

Stable patch-id only shortlists changes. Strict verification supports UTF-8 text
without NUL in regular files. Root/merge/empty commits, binary content, moved paths
and context differences do not become automatic confirmations. Revert and
revert-of-revert clues follow originals, strict matches and verified associations
only within the target history. They neither erase inclusion nor prove current
functional effects. Unrelated reverts and other branches are excluded.

`strict_manifest` v1 stores candidate/matching OIDs, parent OIDs and each file's
canonical `path_b64`, before/after mode, blob OID and SHA-256. Mode `000000` with
null object/digest means absent; an empty file retains its actual blob and digest.
Raw paths compare losslessly; source text is not persisted. One manifest permits
256 total file records, 4096 raw bytes per path and 65536 compact UTF-8 JSON bytes.
Overflow produces incomplete `unknown` / `strict_manifest_limit`, never truncated
strict confirmation. The fact API exposes the complete typed manifest.

Policy also bounds fetch depth, scan/history objects, Git time/output/cache disk,
saved metadata, batch size/bytes and candidate count (at most 1000). Truncation and
incomplete processing are explicit. Rules run in an owned, isolated subprocess:
default one-second wall deadline includes startup and IPC; text, input and output
are bounded, and timed-out workers are killed and reaped. Parent-only imports do
not load in the worker; glob support loads only for path rules. Emulated startup
can exhaust this budget; a configurable demonstration is not a default-budget pass.

Shallow history supports a declared patch window only if no enumerated object is
a shallow boundary. Negative ancestry additionally needs complete history or a
proven reverse-ancestor relation. Parent identities come from raw commit headers.
Budget failures and missing objects never become successful empty evidence.

## Identity, persistence and recovery

Discovery saves a fixed tip, ordered object manifest, cursor and last complete tip.
Fast-forward scans append; rewritten history rebuilds the window. Records and
cursors publish together. Rule matching resumes independently. Oldest-attempt
scheduling prevents repeatedly failing evidence from starving unseen candidates.

Evidence keys bind repository URL, candidate, target side/source, rules, method and
analysis range. Batches additionally bind the candidate collection, source/upstream
identity, version eligibility and budgets. Publication rechecks inputs and loaded
configuration. Each side replaces successful evidence independently; failed
refreshes retain only same-identity facts, original successful time, failure reason
and retained marker. New candidates stay pending. Input changes invalidate reuse.
The oldest successful side time represents a batch; an attempt is not a success.

Storage format 4 adds phase-owned records, scan, evidence and batch collections;
the public snapshot schema stays 1. Analysis occurs outside the snapshot write
lock and repository locks are released before publication. Removing policy clears
owned observations without affecting other phases. Historical evidence has a
bounded tail; object GC is not automatic. Stop the phase and preserve referenced
identities/database backups before rebuilding a cache into a separate directory.

Formats 0/2/3 require the stopped-instance backup/migration procedure in the
[deployment guide](deployment.md). Migration preserves observations/clocks and
rolls back on failure. Older binaries require the verified old backup for rollback.
Strict manifests add JSON fields within format 4; old missing fields read as null.
Method-v1 facts remain readable but cannot be reused as method-v2 evidence.
Repository retries use bounded backoff; package rule failures stay local.

## API, pages and verification

Normal monitor focus/facets support `commits:status`, `commits:repository` and
`commits:rule`, counting packages once per facet over the published subset.
`/api/v2/packages/NAME/commits` accepts `status`, `rule`, `per_page` (1..50) and
`cursor`; the cursor binds the exact batch, filters and OID ordering. Mismatches
return 422 and require page one. `/api/ui/packages/NAME/commits` supplies the shared
document rendered at `/packages/NAME/commits`. Package details show both targets,
reasons, evidence, observation times, stale/retained state and revert clues.

Summary/history share `check`: configured/status/stale, successful `checked_at`,
latest `attempted_at`, collection error and analysis failures. Initial errors and
pending/unconfigured empty pages are distinct from successful zero findings.
Changed/expired inputs stay explicit; failures retain original observation times.
Wait for `/readyz` HTTP 200 before inspecting a newly prepared API projection.

Run commit/storage regressions, architecture/import/type checks, generated API type
checks, frontend quality/check/SSR and full native tests as described in
[CONTRIBUTING](../CONTRIBUTING.md). Native SPEC isolation needs RPM and Landlock
ABI 6+; do not disable isolation or NFS storage guards to pass on an unsuitable host.

The [frozen corpus](../backend/tests/fixtures/commits-real-cases.json) contains 31
QEMU/LLVM cases: 18 development, 13 independent, 12 disjoint modification groups
and 12 nonempty negatives (QEMU 25 / LLVM 6). Schema 4 shares frozen windows,
explanation text, empty defaults and identical strict file lists. Expansion retains
every independent assertion; `source_manifest` identifies the original full raw-Git
annotations by commit and SHA-256. Historical one-off tools remain in that commit.

Prepare the declared objects and parents in isolated bare caches, then run
`python3 scripts/check-commit-cases.py --cache-root DATA --output NEW-REPORT.json`.
Replay stays offline, refuses to overwrite reports and compares identity, status,
reason, matches, reverts, coverage and strict proof (independent of file ordering).
Reports bind source/manifest digests, method and environment. Expectations never
come from the analyzer under test. Machine labels do not establish human semantic
precision/recall or production performance; detailed run reports are delivery artifacts.
