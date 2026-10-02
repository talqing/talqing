# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately, either way:

- Email [hello@talqing.com](mailto:hello@talqing.com) with "Security" in the subject.
- Use GitHub's [private vulnerability reporting](https://github.com/talqing/talqing/security/advisories/new) on this repository.

Include what you found, how to reproduce it, and what an attacker could do with it. We will acknowledge your report within three working days, keep you informed while we fix it, and credit you when the fix ships unless you would rather we did not.

## Scope

- The code in this repository.
- The hosted service at `talqing.com`, `app.talqing.com` and `api.*.talqing.com`.

When testing the hosted service, use only your own workspace and data. Do not run denial-of-service tests, do not place calls or send messages to people who have not agreed to it, and do not access another workspace's data beyond what is needed to show the problem exists.

## Supported versions

Fixes land on `main`. The hosted service runs `main`, and self-hosted deployments should track it.
