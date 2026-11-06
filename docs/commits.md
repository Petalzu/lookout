# Upstream commit evidence

The optional `commits` batch collector records historical upstream inclusion, not
whether a distributed binary is fixed, vulnerable, fast, or suitable for upgrade.
It does not execute upstream code or automatically rebuild arbitrary SPEC files.
Page and API requests read saved snapshots and never run Git or fetch providers.

## Configuration and operation

Repository policy belongs in the release-owned distribution catalog's `[commits]`
table, with operator overrides in the normal distribution override file. Package
rules and reviewed mappings use `monitors.commits` in the package catalog or
package overrides. Like other package monitor identities, each package policy is
replaced as a unit. All loaded files participate in publication drift detection.
No repository or package is enabled merely by installing the feature.

A repository requires a public canonical HTTPS `url`, a public `web_url`, an
explicit `refs/heads/...` monitoring `ref`, and a full immutable `window_start`
commit. `analysis_start`, when supplied, independently bounds the target history
searched for patch and revert evidence. These two ranges are not interchangeable.
`window_start` and `analysis_start` are exclusive lower boundaries. Monitoring
branches discover candidates; only reviewed releases determine upgrade targets.

A package policy requires `repository`, positive `rules`, and optional release
`mappings`. Each rule has a unique `id`, `keywords`, `patterns`, `paths`,
`case_sensitive` and `exclude`. Text alternatives are ORed, path patterns are
ORed, and text intersects paths within one rule. Positive rules are ORed; a
matching exclusion removes the candidate. Text defaults to case insensitive.
Paths use case-sensitive repository-relative globs; `*` does not cross `/`,
`**` is recursive, including hidden paths. Each match retains its rule, field,
fragment and path. Pattern evaluation runs in a bounded owned subprocess.

A reviewed mapping key is the exact release version. Its required fields are
`oid`, `source_revision`, `source_url`, `review_url`, and positive `version`.
An optional `ref` must be an explicit release tag. `source_revision` must match
the current saved SPEC/source revision. For current releases `source_url` must
occur in trusted, confined Source0 evidence. A contradictory trusted full Source0
commit cannot be overridden. Use a public review URL documenting the mapping;
version numbers, homepages and mutable tags alone are insufficient.

Example shape (replace every example identity before enabling):

```toml
[commits]
interval_seconds = 3600
stale_after_seconds = 86400
batch_size = 32
max_findings = 1000
max_batch_bytes = 4194304

[commits.repositories.example]
url = "https://example.org/upstream.git"
web_url = "https://example.org/upstream"
ref = "refs/heads/main"
window_start = "0000000000000000000000000000000000000000"
object_format = "sha1"
fetch_depth = 2048
scan_limit = 2000
history_limit = 2000
timeout_seconds = 60
output_limit = 8388608
disk_limit = 1073741824
record_limit_bytes = 16777216
```

The all-zero object above is deliberately unusable. The repository must contain
an established lower boundary. Package configuration, in its separate catalog:

```toml
[example.monitors.commits]
repository = "example"
rules = [
  { id = "riscv-text", keywords = ["riscv", "risc-v"] },
  { id = "riscv-path", paths = ["target/riscv/**", "hw/riscv/**"] },
]
```

Run the existing collector CLI with `--only commits`, `--config` and `--db`.
The image entrypoint schedules this phase only for configured package policies.
Do not put `commits` in `[monitors].enabled`: that list registers HTTP adapters.
The commits batch monitor deliberately does not change their runner contract.

## States and limits

- `original_reachable`: the original object is in the target's reachable history.
- `strict_patch_evidence`: a target-reachable single-parent text change has the
  same changed file set, raw paths, modes, and complete before/after file hashes.
- `backport_needs_review`: a patch-id or source-object reference provides a clue,
  but strict equivalence is not established.
- `no_evidence_in_range`: the declared range and supported checks completed
  without evidence. This does not prove semantic absence outside that range.
- `unknown`: identity, missing objects/history, budget, or unsupported shape
  prevents a conclusion. The reason and completed range remain visible.

Stable patch-id only shortlists candidates. Empty files and absent files differ.
Merge/root/empty commits, binary or non-UTF-8 content, moved paths and changed
context are not automatically confirmed by the strict method. Revert and
revert-of-revert links are clues in the target history; they never erase the
historical inclusion fact or establish present functional behavior.

A shallow repository can support an explicitly bounded range only when none of
that range's enumerated objects is a shallow boundary. A negative original
ancestry additionally requires complete history or a proven reverse ancestor
relation; a finite patch window cannot prove absence from older target history.
Other shallow negatives stay unknown. Parent identities come from immutable
commit object headers, not the shallow graph's rewritten parent projection. Budget failure does not become successful empty evidence.
Discovery uses a fixed tip, a resumable object list, and a separate complete tip.
Fast-forward collection appends object identities; rewritten history rebuilds
the declared window. Package matching has its own bounded replay cursor.
Evidence keys bind the repository URL, candidate, target side/source identity,
rule/method version, and analysis range; batches separately bind the collection.
Oldest-attempt scheduling prevents permanent failures from starving unseen work.

Defaults bound fetching depth, object enumeration, subprocess time/output,
repository disk and saved metadata, rule input/output, public candidate count
and batch bytes. The public subset is at most 1000 candidates, even when the
scan has more matches. Counts and facets cover that subset, not all Git history.
Truncation, incomplete matching and incomplete analysis remain explicit.
Do not increase limits without measuring the repository and the host budget.

Each target side retains only same-identity evidence after a failed refresh,
with its original time and a retained marker. New candidates remain pending;
changed targets never inherit another target's result. A successful empty check
replaces old evidence. Cached objects do not acquire a new source observation
time merely because they are read again. The oldest successful side time is
shown for the batch. Never interpret `attempted_at` as successful collection.

## API and presentation

Package summaries/details expose the typed `commits` kind. Use the normal
monitor focus and facet query mechanism with `commits:status`,
`commits:repository`, and `commits:rule`. A package is counted once per selected
facet, not once per matching commit. Existing GET unions/intersections apply.

`/api/v2/packages/NAME/commits` returns the published subset with `status`,
`rule`, `per_page` (1..50) and `cursor`. The cursor binds the exact saved batch,
filter signature and stable OID ordering. A replaced batch or filter
mismatch returns 422 with a refresh instruction; restart from page one.
`/api/ui/packages/NAME/commits` returns the equivalent shared document section;
`/packages/NAME/commits` is a thin Astro history page. The normal package detail
shows current/upgrade evidence, targets, observation times and revert clues.
Summary and history both mark stale input or failed refreshes, without relabeling
old facts as a new success. Upstream commit time and collection time differ.

## Storage, failure recovery and compatibility

Storage format 4 adds phase-owned keyed collections for commit records, scan
manifests, evidence entries, and package batches/rule progress. Public snapshot
schema remains 1. Only the `commits` phase owns these fields and its prefixed
components. Fetch/analysis happens outside the database write transaction;
publication rechecks configuration, source, upstream identity and targets.
Repository locks are released before the snapshot writer lock is acquired.

Formats 0, 2 and 3 need the existing stopped-instance backup/migration command.
Migration keeps old observations and clocks and rolls back on failure. Older
binaries cannot read format 4; a rollback requires the verified pre-upgrade
backup, not an in-place downgrade or reuse of the new database. Follow the
existing deployment backup/independent restore procedure.

Failed repository scans retain their prior complete batch and expose the error.
Package rule failures are isolated from sibling packages. Retry uses bounded
backoff; resume the same manifest after a crash. Removing a package/repository
policy removes its active observations at the next collection. Historical
individual evidence has a bounded tail. Bare object storage is monitored against a disk budget;
automatic in-place object GC is not enabled. To replace a cache, stop the phase,
preserve the database backup and all referenced input identities, then rebuild
the declared range into a new cache and revalidate before switching it.

## Validation and environment boundary

Run targeted `backend/tests/monitors/commits` and storage migration tests first,
then the existing architecture/import/type gates, API type generation check,
frontend quality/check/SSR, and complete native image CI. Synthetic Git fixtures
freeze their own inputs and expected conclusions; live versions are not tests.
Public real-case manifests should record repository, full candidate/target OIDs,
independent annotation source, method scope and dataset group/split.

The development root `/nfs/cjl/czx/ospp` on lab-3090-uu is NFS, not an approved
production `/data`. Python 3.14 and Node 26 run from scoped user tools there.
The host Landlock ABI is 1, below the native SPEC requirement of 6. Do not disable
the sandbox or NFS startup guard. Native RPM/SPEC, image entrypoint, migration,
rollback and restore gates require the fork's conforming Actions runner.
Passing local synthetic/SQLite/SSR tests is not full native acceptance.

## Fixed real-case replay

`backend/tests/fixtures/commits-real-cases.json` contains 26 fixed QEMU/LLVM
commit-target cases in 11 modification groups. Fifteen development cases and
eleven independent cases use disjoint groups. Independent non-RISC-V QEMU
controls evaluate inclusion methods, not discovery recall. The bounded LLVM
release window includes a RISC-V path change whose title lacks that token.
Raw commit trailers, ancestor checks, and independent complete before/after
file-byte comparisons document annotations; the analyzer does not generate
the expected states. This small corpus is not an accuracy claim for all upstreams.

Prepare the declared objects and parents in isolated bare caches, then run
`python3 scripts/check-commit-cases.py --cache-root DATA --output NEW-REPORT.json`.
The command is offline, never fetches, does not modify the caches, refuses to
overwrite a report, and records per-case state/coverage/runtime. Missing objects
produce unknown results, not a fabricated pass. Ordinary pytest validates the
manifest structure and group isolation without network access.

## QEMU and LLVM opt-in examples

[Repository policies](../config/commits-distribution.example.toml) and
[package rules](../config/commits-packages.example.toml) use the fixed release
windows exercised by the real-case corpus. They are not automatically loaded.
Merge the `commits` distribution unit and individual package monitor units into
your reviewed catalogs/overrides; retain unrelated package policies. The
examples deliberately omit release-to-source mappings: an operator must obtain
trusted Source0 and the current saved source revision, review the exact OID and
optional immutable tag observation, then add the mapping. Without that evidence
the monitor must show target identity unresolved, not guess from a version.
These narrow release windows are demonstration scope, not full upstream coverage.
