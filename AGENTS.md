# AI-TOKEN Public Skills Repository Rules

## Repository Scope

This repository contains only AI-TOKEN Agent Skills that are intended for external users and cross-tool distribution.

- Keep every published skill self-contained in its own directory with a required `SKILL.md`.
- Keep every private or developer-only Skill library outside this repository.
- Never copy a developer-only skill into this repository until it has passed an explicit public-release review.
- Do not add local project history, internal issue archives, deployment records, production configuration, or private workflow payloads.

## Published Skill Naming

- 后续发布的技能使用中文名称和中文技能根目录名；`SKILL.md` 的 `name` 必须与技能根目录名一致。
- 技能标题、界面显示名、仓库目录清单和安装说明使用一致的中文名称；更名时同步更新路径、技能调用引用及相关集成。
- 保留 `SKILL.md` 等规范要求的文件名；脚本、配置和接口标识按其技术约定命名。

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
