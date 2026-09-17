# Security Policy

## Reporting a vulnerability

**Do not open a public issue.** Report privately through
[GitHub's private vulnerability reporting](https://github.com/EdwardPham1615/py-common/security/advisories/new),
which reaches the maintainer without disclosing anything.

Please include the pinned version, which extras you installed, and the smallest
reproduction you can manage. If you are unsure whether something is a
vulnerability, report it anyway — deciding that is the maintainer's job, not the
reporter's.

Expect an acknowledgement within a week. This is a small project with one
maintainer, so please allow reasonable time for a fix before publishing.

## Supported versions

Only the latest tag receives fixes. This library is at `0.x` and consumers pin a
tag, so the practical remedy for a security fix is to move to the newest one.

## Scope

py-common is a library, not a service: it has no deployment of its own and holds
no data. The vulnerabilities that matter here are ones a consuming service would
inherit — an auth check that passes when it should not, a header parsed from an
untrusted source and trusted, credentials or tokens reaching logs, or a default
that is insecure without saying so.

Dependency advisories are already tracked: CI runs `pip-audit` against the
locked dependency set on every push, and Dependabot opens upgrade pull requests.
You are welcome to report one anyway if it looks unhandled.
