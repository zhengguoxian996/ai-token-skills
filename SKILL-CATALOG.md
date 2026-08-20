# AI-TOKEN Public Skills

本目录只维护可供用户下载和安装的 AI-TOKEN Agent Skills。

## 发布边界

- 本目录是唯一允许初始化 Git 并面向用户发布的技能目录；开发技能目录保持在本仓库之外。
- 技能必须自包含、可移植，并通过结构校验和最小行为测试。
- 不得包含开发者本地绝对路径、服务器配置、内部知识库、密钥或用户私有数据。
- Codex 专属的 `agents/openai.yaml` 可以作为可选适配存在，但 `SKILL.md` 的核心流程不得依赖单一工具。
- 只有完成公开化审查的技能才能提交和推送。
- 目录归属不等于操作授权；每次提交和推送仍需要用户对当次任务明确授权。

## 当前技能

### `ai-token-cloud-comfyui`

- 面向用户直接探测、校验、执行和监控 AI-TOKEN 云端 ComfyUI API 工作流。
- 核心流程不依赖单一 Agent 工具；Codex UI 元数据作为可选适配保存在 `agents/openai.yaml`。
- 保留提交次数、授权、隐私和直连边界，并记录已验证的最小执行基线。

## 计划技能

### `ai-token-cloud-comfyui-lifecycle`

- 负责云端 ComfyUI 生命周期操作及其授权、停止条件和资源安全边界。
- 尚未创建。
