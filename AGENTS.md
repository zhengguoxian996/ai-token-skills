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

## Stable Skill Identity

- 每个发布技能在 `SKILL.md` 的 `metadata.skill_id` 中声明一个创建时生成的 UUID，并在更新、改名和移动目录时永久保留。
- 技能 ID 与名称、目录、版本号、Git 提交和内容摘要分别维护；不得从这些可变信息重新生成身份 ID。
- 新建独立技能必须使用新 ID，不得复制其他技能的 ID。发布前检查 ID 格式及仓库内唯一性。
- 已发布的旧技能首次补 ID 时，应明确绑定原市场记录；不得按相似名称自动合并技能或转移用户权益。

## Skill Versions

- 后续创建和发布的技能必须在 `SKILL.md` 的 `metadata.version` 中声明带引号的三段式正式版本号 `主版本.次版本.修订号`，首次版本为 `"1.0.0"`；每段使用不带前导零的非负整数。
- 修复递增修订号，兼容功能增加递增次版本号，不兼容变更递增主版本号。发布内容发生变化时必须提高版本号，同一版本号不对应不同发布内容。
- 升级时保留 `metadata.skill_id`，只更新版本和内容。同步以固定 ID 绑定原技能，版本按数值段识别升级，结合发布记录、内容摘要和本地修改状态判断覆盖，并保留用户配置与任务日志。
- 不得使用 Git 提交、文件夹名或修改日期替代技能版本号；发布前校验缺失和非法版本。

## Commit and Push Rules

- Files inside this repository may be committed only after public-release review and validation.
- A task must explicitly authorize the commit before creating it.
- A task must explicitly authorize the push and identify the remote before pushing.
- Do not initialize Git in the developer skills directory and do not move that directory under this repository.

## Public-Release Checks

Before committing a skill:

1. Validate its structure with the Agent Skills validator.
   Run `python scripts/validate_skills.py` to also check required release versions, fixed UUIDs and repository-wide ID uniqueness.
2. Confirm that all required references, scripts, and assets are included.
3. Remove developer-specific absolute paths, server details, secrets, credentials, cookies, dynamic service addresses, user content, and internal knowledge-base references.
4. Confirm that the core `SKILL.md` workflow does not require Codex-only metadata or one specific agent tool.
5. Review `git diff --check`, the staged diff, and the complete staged file list.

## Authorization Boundary

Repository membership means a skill is eligible for publication; it does not authorize an automatic commit, push, release, network request, paid execution, or external mutation.
