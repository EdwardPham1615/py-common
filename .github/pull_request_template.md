<!--
Guidelines: CONTRIBUTING.md
Delete sections that do not apply. Do not delete "Why".
-->

## What

<!-- One or two sentences on what this changes. -->

## Why

<!--
The part reviewers need most. What was the behaviour before, what does it break,
and why this fix rather than another? A reviewer who understands the failure can
tell you the fix is wrong; one who only sees the diff can only say it looks fine.
-->

## How it was verified

<!--
Which tests fail without this change? For a bug fix, a test that reproduces the
original failure -- not just one that exercises the new code path.
-->

## Breaking changes

<!--
None, or: what breaks, and the migration. Show before and after. A consumer
should be able to follow it without reading the diff.

This library is shared by several services, so a breaking change is an upgrade
someone else has to absorb.
-->

None.

---

- [ ] `make check` passes locally
- [ ] Tests added that fail without this change
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (skip for internal refactors and test-only changes)
- [ ] Breaking changes marked **BREAKING** with a migration note
- [ ] `py_common.testing.fakes` updated if an interface changed
