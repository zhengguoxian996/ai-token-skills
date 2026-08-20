# AI-TOKEN Public Skills Repository Rules

## Repository Scope

This repository contains only AI-TOKEN Agent Skills that are intended for external users and cross-tool distribution.

- Keep every published skill self-contained in its own directory with a required `SKILL.md`.
- Keep every private or developer-only Skill library outside this repository.
- Never copy a developer-only skill into this repository until it has passed an explicit public-release review.
- Do not add local project history, internal issue archives, deployment records, production configuration, or private workflow payloads.

## Commit and Push Rules

- Files inside this repository may be committed only after public-release review and validation.
- A task must explicitly authorize the commit before creating it.
- A task must explicitly authorize the push and identify the remote before pushing.
- Do not initialize Git in the developer skills directory and do not move that directory under this repository.

## Public-Release Checks

Before committing a skill:

1. Validate its structure with the Agent Skills validator.
2. Confirm that all required references, scripts, and assets are included.
3. Remove developer-specific absolute paths, server details, secrets, credentials, cookies, dynamic service addresses, user content, and internal knowledge-base references.
4. Confirm that the core `SKILL.md` workflow does not require Codex-only metadata or one specific agent tool.
5. Review `git diff --check`, the staged diff, and the complete staged file list.

## Authorization Boundary

Repository membership means a skill is eligible for publication; it does not authorize an automatic commit, push, release, network request, paid execution, or external mutation.
