# AI-TOKEN Agent Skills

面向 AI-TOKEN 用户的跨工具 Agent Skills 仓库。

## 可用技能

- [`ai-token-cloud-comfyui`](ai-token-cloud-comfyui/SKILL.md)：直接检查、校验、执行和监控用户提供的 AI-TOKEN 云端 ComfyUI API 工作流。

## 安装

将需要的完整技能目录复制或链接到所用 Agent 工具支持的 Skills 目录。保留技能目录中的 `SKILL.md` 以及随附的 `agents`、`references`、`scripts` 或 `assets`；具体发现与安装方式以所用工具的文档为准。

`agents/openai.yaml` 是 OpenAI/Codex 的可选界面适配，不是核心技能流程的运行前提。

## 安全说明

- 使用前阅读目标技能的 Required Inputs 和 Safety Boundaries。
- 云端地址、工作流、用户文件和凭据不得提交到本仓库。
- 付费或消耗算力的执行必须获得明确授权，并设置最大提交次数。
