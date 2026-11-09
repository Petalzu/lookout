# Contributing

For package data, start with [Configuration](config/README.md). For an additional
monitor, follow [Monitor porting](docs/monitor-porting.md).

## Where to edit

| Change | Entry point |
|---|---|
| Package version rules, comparison policy or monitor identities | [Configuration](config/README.md) |
| Collect a monitor's observations | `backend/tracker/monitors/<name>/` or `<name>.py` |
| Register an evidence adapter | `backend/tracker/monitors/registry.py` |
| Schedule checks and publish their results | `backend/tracker/monitors/runner.py`, `schedule.py` |
| Access external registry data | `backend/tracker/providers/` |
| Derive package state, filters and counts | `backend/tracker/readmodel/` |
| Select and group visible facts | `backend/tracker/presentation/` |
| Render documents and controls | `frontend/src/components/document/`, `pages/`, `styles/` |
| Local brand assets and provenance | `frontend/src/assets/logos/catalog.json`, with BuildSystem mappings in `config/distribution.toml` |
| Persist state or change HTTP contracts | `backend/tracker/state.py`, `api.py` |

Tests follow these responsibilities under `backend/tests/`. Shared fixtures belong
in `conftest.py` or `helpers/`, not in another test module. See [Design](docs/design.md)
for write ownership and trust boundaries.

For artwork provenance and usage terms, see [Interface artwork](docs/assets.md).
Use explicit identity mappings, not name guessing or remote icon services. Unknown icons remain text.

## Run checks

Local checks require Python 3.14+ and a Node version matching
[`frontend/package.json`](frontend/package.json) `engines`. `npm ci` rejects
unsupported versions. Native tests also require Linux,
RPM bindings, Landlock ABI 6+ and seccomp. Install the backend `test` and `quality`
extras (or the test lock), then run `npm ci` in `frontend`:

```sh
python3 scripts/check-architecture.py
python3 scripts/api-types.py --check
(cd backend && ruff check tracker nvchecker_source && pyright && lint-imports --no-cache && deptry .)
(cd backend && vulture tracker nvchecker_source --min-confidence 100 && python3 -m pytest -q)
(cd frontend && npm run quality && npm run check && npm test)
```

For local coverage, run `(cd backend && pytest --cov --cov-report=term)`.
Add `--cov-report=xml` to write `backend/coverage.xml`.

Then run [Image checks](#image-checks).

If an expected pull-request run is absent, dispatch the `CI` workflow on the
exact feature branch. Manual runs execute the full non-release gate without
publishing images, merging changes, or creating a Release. Record the run SHA;
a successful run on an older commit is not validation of new changes.

After changing response models, run `python3 scripts/api-types.py` and review the
resulting `frontend/src/lib/api.generated.ts`. `openapi-typescript` compiles the
reachable UI schemas. `src/lib/document.ts` selects display primitives. Do not
hand-edit generated types or copy provider models into components.

Keep generated schema exports and the built SSR entry out of dead-code removal.

Use `radon cc backend/tracker -s -n C` to locate complex functions. Treat scores and
lower-confidence dead-code reports as investigation input, not deletion approval.

Run `(cd frontend && npm run duplicates)` for copy/paste candidates, or
`npm run duplicates:similar` in the same directory to include renamed blocks.
The root `.jscpd.json` selects hand-written production code. It excludes tests and generated API types. Duplication percentages are advisory. Extract shared rules only when both copies must change together.
Imports, provider policies and independent upgrade entrypoints may stay separate.
Keep release behavior and project-owned contract regressions in the suite.
Third-party tool evaluations, refactor comparisons and one-off audit experiments
belong outside the source tree, not in permanent regression tests.

Declare dependencies in `backend/pyproject.toml`. Update the runtime and test
locks together when changing them (`uv` is a development tool):

```sh
uv pip compile backend/pyproject.toml --python-version 3.14 --python-platform linux --no-header --no-annotate -o backend/requirements.lock
uv pip compile backend/pyproject.toml --extra test --extra quality --python-version 3.14 --python-platform linux --constraint backend/requirements.lock --no-header --no-annotate -o backend/requirements-test.lock
```

Regression tests fix their inputs, not today's upstream versions or package
counts. Test shipped configuration against schema and policy. Freeze semantic
clocks. Synchronize concurrent tests with events rather than elapsed sleeps.

### Image checks

Build and test one image, not a running production instance:

```sh
IMAGE=openruyi-lookout:test
VERSION=$(python3 -c 'import tomllib; print(tomllib.load(open("backend/pyproject.toml", "rb"))["project"]["version"])')
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --build-arg RELEASE_VERSION="$VERSION" -t "$IMAGE" -f Containerfile .
CONTAINER_ENGINE=docker deploy/check-image.sh "$IMAGE"
CONTAINER_ENGINE=docker python3 deploy/smoke-image.py "$IMAGE"
python3 deploy/smoke-release.py "$IMAGE"
```

The container build pins the official Node image by digest and copies the same
Node/npm into the Fedora runtime. The test image inherits them. Update that pin
in `Containerfile` when upgrading Node.

Set `COVERAGE_OUTPUT_DIR` to a host directory before calling `check-image.sh` to
collect `backend-coverage.xml`, a Cobertura report with repository-relative file paths. The script exports it
from the read-only, offline container without a writable host mount. CI enables
coverage; leave the variable unset for a run without coverage.

CI always generates coverage and preserves the report artifact after successful
native checks. The separate GitHub Code Quality upload requires that service to
be available for the repository. Forks opt in with the Actions repository
variable `LOOKOUT_CODE_QUALITY_ENABLED=true` after enabling Code Quality; the
non-fork default is unchanged. This only controls the external report integration,
not tests, coverage generation, artifact upload or the required `CI result` gate.
An enabled upload still fails on authentication, validation or transport errors.

CI reuses build layers, but runs tests on every code change. Only the document
paths listed in `scripts/ci-scope.py` skip image checks. Release always runs them.
Use `CI result` as the required branch check. PRs read shared caches. Only trusted
`main` runs write them. Weekly builds refresh system dependency layers.

The native gate runs without provider network access and rejects skipped tests.
Building its disposable test layer requires the package index. The runtime image
omits test tools. The entrypoint and release tests cover persistent mounts,
installation, migration, failed-upgrade rollback and an independent restore.

The manual Release workflow publishes the same tested image after these gates pass.
Ordinary pushes and pull requests only run CI. See [release instructions](docs/deployment.md#publish-a-release). Publication targets linux/amd64. Additional
architectures require their own native and entrypoint checks. Package owners must
make the GHCR package Public for anonymous pulls. Target-host isolation, real
providers, HTTPS, reboot recovery and off-host restore drills require separate
operator acceptance. See [Deployment](docs/deployment.md).

## Dependency updates

Use the newest stable versions compatible with the tools that consume them.
Update the controlling tools first, then select dependencies within their combined
supported ranges. Update unrelated dependencies independently.

For the frontend, Astro and its Node adapter must work together. TypeScript must
satisfy `@astrojs/check`, TypeScript ESLint and `openapi-typescript`. Use declared peer dependencies
and runtime requirements as constraints. CI must also pass.

Keep compatibility ranges in manifests and resolved versions in lock files.
Review these ranges when a controlling tool gains support for a newer dependency.
Do not bypass conflicts with `--force`, `--legacy-peer-deps`, or disabled checks.

[Dependabot](.github/dependabot.yml) proposes npm and GitHub Actions updates.
Its groups batch changes. They do not prove compatibility or apply this policy automatically.
Review related updates together. Keep incompatible updates unmerged until their
consumers support them. Do not block all major updates to avoid one conflict.
For security fixes, upgrade the affected toolchain or use a reviewed mitigation.

## Documentation and comments

Keep instructions in their task guide and link to them elsewhere.
Place runnable examples beside their explanations. State results and failure handling.
Check commands, paths and links against the implementation.

Use one term for each concept. Write one instruction per sentence.
State conditions before actions. Name the component that performs each action.
Aim for at most 20 words per instruction and 25 words per descriptive sentence.
Keep technical names, command syntax and safety conditions exact.
Put required actions in steps, not notes.

Comments explain protocol exceptions, ownership or why a simpler implementation
would be wrong. Do not narrate code. Docstrings document a caller's non-obvious
contract, side effects and failures. Use synthetic examples rather than assertions
about today's provider versions or package counts.

## Request review

Submit one focused change. Review all code and text, including generated content.
Briefly identify substantial tool-generated material without attaching chat logs.
Keep required lock files, fixtures and license copies. Automated agents need
human approval before publishing issues, review comments or batches of PRs.

Describe the problem and reproduction, why this fix is sufficient, checks actually
run, and anything unverified or requiring a maintainer's decision. Do not retell
the diff or report someone else's test results as your own.

The frontend pins `http-cache-semantics` with an install/build-time security patch for
GHSA-ch52-4w7c-c8xp. Its source checksum rejects unreviewed upgrades. Remove the
patch only when `frontend/tests/http-cache.cjs` passes against unmodified upstream.
