# Monitor configuration

## Find the owner

| Change | File / table |
|---|---|
| Upstream identity, release filter, version line | [`versions/nvchecker.toml`](versions/nvchecker.toml), by track name |
| Compare/watch policy or monitor identity exception | [`packages.toml`](packages.toml), by package name |
| OBS targets, SPEC repository, intervals, enabled monitors | [`tracker.toml`](tracker.toml) |
| BuildSystem colors and icons | [`distribution.toml`](distribution.toml), `[buildsystems]` |
| Dependency → source-package mappings | `distribution.toml`, `[dependencies]` |
| Dependency target environments | `distribution.toml`, `[dependency_environments]` |

Catalog references accept absolute paths or paths relative to `tracker.toml`.
Installation references the image's `/app/config/` catalogs, rather than copying
them into `/config`. Release updates therefore supply new identities, rules and
distribution mappings without replacing operator settings.
Absolute references use the application container's filesystem. The installer
initializes them inside the selected image. Do not use absolute host-checkout paths as container paths.

Administrator exceptions use separately named files:

```toml
# /config/tracker.toml (root settings, before table headers)
package_overrides = "package-overrides.toml"

[collector]
version_overrides = "version-overrides.toml"
exclude_tracks = ["intentionally-disabled-track"]
```

`version-overrides.toml` uses the same nvchecker syntax: a track replaces its whole
default rule. `__config__` overrides individual checker options. A relative
`keyfile` resolves beside the file that declares it. `package-overrides.toml`
overrides individual policy fields and complete monitor identities:

```toml
widget = { monitors = { security = { vendor = "verified_vendor", product = "verified_product" }, eol = false } }
```

This leaves other widget monitors intact. `false` disables that monitor, including
automatic identity derivation. Local `[openruyi]` tables override individual entries
from `distribution.toml`. Unreferenced sibling files are never loaded.

Keep operator config and credentials outside the checkout.
For old copied catalogs, follow [ownership migration](../docs/deployment.md#copied-catalog-migration), not a replacement with repository defaults.

Locate the effective rule and identity **without contacting providers**:

```sh
PYTHONPATH=backend python -m tracker.package explain python-requests \
  --config config/tracker.toml --format human
PYTHONPATH=backend python -m tracker.monitors explain openssl --monitor eol \
  --config config/tracker.toml --format human
```

Add `--db /path/to/snapshot-copy.sqlite3` to monitor explain for saved Source0,
check and due-time context. Edit provider code via [Contributing](../CONTRIBUTING.md#where-to-edit).

## Version rules

Use one native inline table per track. Quoted keys retain dots and `@` in names:

```toml
python-requests = { source = "pypi", pypi = "requests" }
"widget@3" = { source = "pypi", pypi = "widget", include_regex = '^3\.[0-9]+\.[0-9]+$' }
```

The package name defaults to the track name. Compare formal releases. Preserve explicit maintenance lines. Shared KDE/Qt homepages do not establish component identity.

For a prerelease side observation, define another track and bind it without
changing the main comparison:

| `versions/nvchecker.toml` | `packages.toml` |
|---|---|
| `"widget@prerelease" = { source = "pypi", pypi = "widget", use_pre_release = true }` | `widget = { watch = ["widget@prerelease"] }` |

Run a single-package provider check without writing the production snapshot:

```sh
PYTHONPATH=backend python -m tracker.package check python-requests \
  --config config/tracker.toml --format human --output /tmp/requests-check.json
```

`--format human` prints a short result. The default is JSON. `--output` saves the
complete report. Compare rule identities and filters, not just their current results.

### Supplied nvchecker sources

The backend installs these [plugins](../backend/nvchecker_source/) alongside
nvchecker. Their rules require that backend, not a bare nvchecker installation.

| Source | Identity | Selection |
|---|---|---|
| `crates_index` | `cratesio = "accesskit"` | Sparse-index versions excluding yanked releases |
| `go_proxy` | `url = "…/@latest"` | Formal Go releases, excluding prereleases and pseudo-versions |
| `anitya_stable` | `anitya_id = 7306` | First provider-ordered `stable_versions` value |

`crates_index` and `anitya_stable` accept a custom `url` **instead of** their identity
field. Common nvchecker filters/normalization still apply. To filter an Anitya
maintenance line **before** taking the first result, use native `jq`. Filtering
after `anitya_stable` would discard the provider's other candidates.

```toml
"widget@3" = { source = "jq", url = "https://release-monitoring.org/api/v2/versions/?project_id=PROJECT_ID", filter = 'first(.stable_versions[] | select(test("^v?3[.]")))', prefix = "v" }
```

Replace `PROJECT_ID` with a verified identity. Keep the provider's ordering.
Do not sort Anitya history with nvchecker's generic comparator.

### Git snapshots

A complete Source0 commit must agree with the RPM `+gitDATE.shortsha` identity.
A name or prefix alone is insufficient. Check the branch from repository refs:

| `versions/nvchecker.toml` | `packages.toml` |
|---|---|
| `"widget@commits" = { source = "git", git = "https://github.com/example/widget", use_commit = true, branch = "main" }` | `widget = { compare = "widget@commits", watch = ["widget"] }` |

The old release rule stays as a watch. Different commits mean `changed`, not a
proven newer release. The list uses `YYYYMMDD.xxxxxx`. Target dates come only from
provider `revision_creation_time`, so plain Git without that field shows a short
hash. The formatter extends colliding prefixes. Detail/API retain full identities.
Commits do not become License/Requires upgrade-version inputs.

## Other monitor identities

Registry-backed monitors reuse saved Source0 identity, then native-rule identity.
Explicit settings take precedence. EOL needs a product and cycle policy:

```toml
openssl = { monitors = { eol = { product = "openssl", cycle_parts = 2 } } }
widget = { monitors = { license = { pypi = "upstream-widget" } } }
```

| Identity | Reused by |
|---|---|
| PyPI | Security, License, runtime requirements, Yanked |
| crates.io | Security, License, Yanked. Toolchain requirements are not runtime dependencies |
| Go module | Security, deps.dev license, proxy withdrawal, explicit `go.mod` language minimum (build-only) |
| CPAN distribution | License and static runtime prerequisites. Module and distribution versions differ |
| Complete Source0 commit | OSV commit query. Repository matches do not establish subpackage applicability |
| Version-matched Source0 archive tag | OSV GIT repository/tag query |
| Reviewed CPE part/vendor/product | NVD CVE API. The provider matches current-version ranges |

For traditional software without a registry identity, configure the CPE explicitly:

```toml
widget = { monitors = { security = { vendor = "verified_vendor", product = "verified_product" } } }
```

The default CPE part is `a` (application). Use `part = "o"` for an OS kernel.
Check identity against upstream sources and the NVD CPE dictionary. Matching package names alone do not establish identity. NVD rejects unresolved/snapshot versions instead of
stripping them to a release. Lookout spaces public API requests 6.5 seconds apart.
Checks use the shared dated cache and retry automatically after errors.

Missing mappings remain unknown. `NotPackaged` requires a reviewed mapping absent
from a fresh inventory. PEP 508 conditions use the configured target, never the
collector host. Dynamic CPAN prerequisites and ambiguous metadata stay unsupported.
Adapter-specific fields and new backends are in [Monitor porting](../docs/monitor-porting.md).

## Add a package

The SPEC catalogue supplies package rows. OBS supplies build observations. Offline `setup`
currently proposes rules only from crates.io Source0 evidence:

```sh
PYTHONPATH=backend python -m tracker.package setup PACKAGE \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/package-proposal.json
```

Review identity and release policy before adding the entry. `entry=null` means that setup found no candidate. Inspect `reason`, not just the count. Aliases belong in
identity data. A SPEC Name/directory mismatch is a packaging defect: an exception
must state its `TODO(drop)` removal condition.

Broader discovery may contact Anitya. `--verify` additionally runs nvchecker.
Neither operation changes running configuration:

```sh
PYTHONPATH=backend python -m tracker.monitors.version.discover \
  --config config/tracker.toml --db /path/to/snapshot-copy.sqlite3 \
  --output /tmp/version-candidates --verify
```

Keep exact prerelease Source0 identities for registry queries even if RPM Version
normalizes their spelling. Review discovery proposals before use.

## Promote reviewed changes

Keep the original `BASE`, edited `CANDIDATE` and deployed `RUNTIME` directories.
`REVIEW` and `PREPARED` below must not exist:

```sh
PYTHONPATH=backend python -m tracker.package plan \
  --base-config "$BASE/tracker.toml" --config "$CANDIDATE/tracker.toml" \
  --runtime-config "$RUNTIME/tracker.toml" --output "$REVIEW" --format human
# Review the generated file and semantic differences before applying:
PYTHONPATH=backend python -m tracker.package apply \
  --review "$REVIEW" --runtime-config "$RUNTIME/tracker.toml" \
  --destination "$PREPARED" --format human
```

Conflicts or input drift reject promotion. It handles version rules, package
policy, BuildSystem styles and monitor settings. Other operator settings, including
dependency mappings, need explicit edits in the new config directory. Apply creates
a new directory. It does not change the service's mounted configuration. Follow
[Configuration changes](../docs/deployment.md#configuration-and-ports) to switch to
that directory.

For installed configurations, plan/apply edits local overrides, not `/app/config`.
All three inputs must reference the same release catalog. Catalog drift requires a new review.

## SPEC inputs

Failed repository refreshes retain confirmed SPEC revisions and their collection
times. Overdue refreshes mark results as retained; elapsed time alone does not
invalidate version comparisons or exact-release monitor inputs. Changed inputs,
identity conflicts and parse failures still invalidate affected conclusions.

`[spec].extra_macro_packages` selects macro packages in the managed Git tree.
`[spec.local_sources]` maps files needed for native `%include`, such as a patch
series. Their hashes invalidate parse caches. Missing inputs fail the parse.
The parser does not download Source archives or replace RPM expansion with text guessing.

## GitHub activity

Repository defaults are in `distribution.toml` and follow release updates.
To replace them, configure `[github]` in `tracker.toml`. Set `repositories = {}` to
disable collection. Explicit settings are never merged with default repositories:

```toml
[github]
interval_seconds = 600
stale_after_seconds = 1800
request_budget = 120
reconcile_seconds = 604800

[github.repositories."owner/packaging"]
source_root = "SPECS"
ambiguous_names = ["file", "patch"]
aliases = { python-zmq = ["pyzmq"] }
```

PR changed paths (including renamed paths), exact title/body names and
`package:NAME` labels associate records with known source directories. Short
names and `ambiguous_names` require a code span, title prefix or package label.
Aliases must identify one existing package.

SQLite retains open and closed records independently of package versions. Incremental polls use `updated_at`, a two-minute overlap and ETags.
Weekly reconciliation detects missing records. Polls reuse unchanged records and PR paths. Weekly reconciliation rechecks open PR revisions.
Each list page processes issues before PRs. Association checks use PR file paths first, then bounded package-name matching in text.

Each batch starts at most `request_budget` requests within a 120-second window. Requests run
serially, at least one second apart. Anonymous batches start at most every ten
minutes and cannot exceed 60 requests; response quota headers can stop them earlier. A completed poll with no changes adds one interval to the
next delay. Changes reset it. Errors and incomplete batches never count as an
empty poll. Authenticated polls retain the configured fixed interval. The next due time, list page and PR file-page progress survive
restarts. The collector publishes file lists only after checking that the base/head revisions did not change. A growing idle delay also extends the freshness deadline.

With a read-only token, polls use the configured budget and interval.
See [deployment credentials](../docs/deployment.md#listener-and-github-credentials) for secure injection and rotation.
Quota headers reserve 50 authenticated or two anonymous requests for other consumers.
GitHub cooldowns override the schedule. A rejected token (401 or explicit token
permission failure) retries the public endpoint once without credentials. The
collector records a one-hour authentication cooldown; restarts retain it.
An authentication probe does not reset the anonymous poll delay, quota wait or cursor.
Only a successful authenticated request resumes authenticated collection immediately.
Rate limits and network failures do not trigger anonymous retries. Anonymous access shares the host
IP quota, so initial history may take days. GitHub returns at most 3,000 files
per PR. Incomplete paths remain explicit rather than becoming an empty match.

Credentials are not written to observations. Homepage counts show packages.
Package badges count linked records across all states. `/api/v2/packages/NAME/activity?kind=pr` (or `issue`) returns
20 records, newest updated first, with `next_cursor`. `per_page` is at most 50.

### Multi-module Go sources

For packages with a Go identity, the requirements monitor discovers submodules
from saved GitHub Source tag archives (`module/path/vX.Y.Z`). It reads the module
identity and Go minimum from each upstream `go.mod`. Established single-module
identities keep the proxy path, including upgrade checks. Source numbers and download
filenames are not association keys. New or removed archives change the query.

For a bundled module without its own source tag, set
`monitors.requires.go.include` to `{module, from_module, path}` entries.
`from_module` names a discovered module whose source archive contains `path/go.mod`.
The declared module identity must match. Duplicate identities or missing anchors
fail the observation; the monitor does not select the highest or first version.

The result covers discovered source-tag modules and explicit includes, not every
module present in a repository or installed RPM. All must validate before their
highest Go minimum is published. Current archives cannot describe an upgrade or
establish independent component versions for Security or Yanked queries.

### Upstream commit evidence

The optional independent `commits` batch monitor uses reviewed repository and
package policies. See [Commit evidence](../docs/commits.md) for fixed source
mappings, rule semantics, budgets, API queries, and interpretation limits.
It is not an entry in the HTTP-adapter `[monitors].enabled` list.
