# Releasing pycommon

Releases are **tags on `main`**. Consumers install by pinning a tag:

```bash
uv add "pycommon[all] @ git+https://github.com/EdwardPham1615/pycommon.git@v0.1.0"
```

That makes a tag the artifact people actually run, so it is treated as
immutable: **never move or delete a published tag.** A consumer who pinned it
would silently get different code on their next lockfile refresh, with nothing
in their diff to show it. If a release is wrong, publish the next patch.

## Versioning

[Semantic Versioning](https://semver.org/). While the major version is `0`,
SemVer allows a **minor** bump to break compatibility — so a release with
breaking changes goes out as `0.2.0`, not `1.0.0`.

Reach `1.0.0` when you are ready to commit to a stable public API. After that,
breaking changes require a major bump, and the cost of each one goes up sharply.

Every breaking change carries a migration note in [CHANGELOG.md](CHANGELOG.md).
This library is shared by several services; a breaking change without a
migration note becomes someone else's outage during an upgrade they thought was
routine.

## Steps

1. **`main` is green.** Check the latest CI run on `main`, not just your PR —
   including the `audit` job, which can go red on its own as advisories are
   published. Shipping a tag with a known vulnerability in the lockfile is worth
   a few minutes' delay to upgrade.

2. **Close the changelog section.** In [CHANGELOG.md](CHANGELOG.md), rename
   `## [Unreleased]` to `## [0.2.0] - YYYY-MM-DD` and open a fresh empty
   `## [Unreleased]` above it.

3. **Bump the version** in `pyproject.toml` (`[project] version`).

4. **Update the pinned tag in `README.md`.** It appears in more than one place
   (the uv and pip install lines, and the extras example). This is the easiest
   step to forget, because nothing fails when you do — the README simply keeps
   telling new users to install a version you no longer intend them to use.

5. **Open a PR** titled `Release 0.2.0` with those three files, and merge it
   once CI passes.

6. **Tag `main`** at the merge commit:

```bash
git checkout main && git pull && git tag -a v0.2.0 -m "Release 0.2.0" && git push origin v0.2.0
```

7. **Watch the release workflow.** [`release.yml`](.github/workflows/release.yml)
   re-runs lint, typecheck and tests against the tag, builds the sdist and
   wheel, and creates the GitHub Release with both attached. It fails the run if
   the tag does not match the version in `pyproject.toml`, or if `CHANGELOG.md`
   has no section for it — the two mistakes that are invisible until a consumer
   hits them.

8. **Tell the consuming services** what changed, especially if the release has a
   migration note. Nobody upgrades a pinned dependency they have not heard about.

## Patching an older line

Only needed when a service is pinned to an older minor and cannot take the
current one. Otherwise fix it on `main` and release a normal patch.

Branch from the **tag**, not from `main`:

```bash
git checkout -b release/0.1 v0.1.0
```

Fix, add a changelog entry under a new `## [0.1.1]` heading, bump the version,
tag `v0.1.1`, then forward-port the fix to `main` with `git cherry-pick` so the
next release does not regress it. Keep the branch afterwards — deleting it
orphans nothing, but it is the base for any further `0.1.x` patch.
