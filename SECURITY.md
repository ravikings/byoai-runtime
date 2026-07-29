# Security Policy

## Supported Versions

`byoai-runtime` is currently in alpha (`0.x`). Security fixes are made against the latest
release on the `main` branch; there is no long-term support window until a stable `1.0`.

| Version | Supported |
| ------- | --------- |
| latest `0.x` on PyPI | ✅ |
| older `0.x` releases | ❌ |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, use GitHub's private reporting flow:

1. Go to the [Security tab](https://github.com/ravikings/byoai-runtime/security/advisories/new) of this repository.
2. Click **"Report a vulnerability"** to open a private advisory with the maintainers.

Include as much detail as you can: affected version, a reproduction case, and the potential
impact (e.g. credential exposure, cache/namespace isolation bypass, SSRF via a provider adapter).

We'll acknowledge reports as soon as possible and keep you updated as we investigate and fix
the issue. Once a fix is released, we'll coordinate disclosure and credit you in the advisory
(unless you'd prefer to stay anonymous).

## Scope

Given the project's design goals — connecting directly to a caller's existing Redis, vector
store, and LLM infrastructure — the areas we care about most are:

- Cache key namespace isolation (`byoai:*` writes never leaking into or clobbering existing
  application keys).
- Correctness of the AST filter translator (no filter-injection that widens a query beyond the
  caller-supplied predicate).
- Handling of provider credentials and API keys (never logged, never included in error messages
  or telemetry spans).
