# Security Policy

## Supported versions

AutoClipper is pre-release and has no versioned releases yet. Security fixes are applied to `main` only.

## Reporting a vulnerability

**Don't open a public issue for security problems.** Instead:

1. Use GitHub's **private vulnerability reporting** (*Security* tab → *Report a vulnerability*) if it's enabled, or
2. Contact the maintainer privately through [@adillpickles](https://github.com/adillpickles).

Include what you found, how to reproduce it, and the impact you expect. We aim to acknowledge reports within **3 days**.

## Leaked credentials

If you commit a secret (Twitch client secret, OAuth token, API key, etc.), even to a branch that was never merged:

1. **Revoke or rotate it immediately** in the provider's console. For Twitch, regenerate the client secret in the [Twitch Developer Console](https://dev.twitch.tv/console). Treat the old secret as compromised: rewriting git history does not un-leak it.
2. Tell the maintainer.
3. Remove it from the branch. If it reached `main`, the maintainer decides whether to rewrite history.

## Required practices for implementation

- Secrets live only in a local `.env` (git-ignored). `.env.example` documents the variable names with no values.
- Library code reads credentials through the config module only, never from hard-coded strings.
- Logs never include tokens, secrets or raw chat usernames.
- Downloaded media is treated as untrusted input. It's processed with FFmpeg and media libraries, never executed, and stays out of the repository.
- New dependencies are justified in PRs and kept to well-maintained packages.

Secret scanning and push protection are enabled (verified 2026-10-13). Vulnerability alerts and private reporting remain OQ-12 follow-ups (see [`docs/project/open-questions.md`](docs/project/open-questions.md)); no security setting is changed by this repair.
