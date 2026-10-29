---
name: 图片生成-ai-token
description: 使用用户的 ai-token.vip API Key 生成图片，或上传参考图按提示词编辑图片；选择线路、模型、积分与参数，保存本地结果，并查询或恢复原任务。用户提到“图片生成（Ai-token）”、AI-TOKEN 生图、图生图、换背景、改图或多图融合时使用；云端 ComfyUI 工作流另行处理。
metadata:
  display_name: "图片生成（Ai-token）"
  category: "基础/图像生成"
---

# 图片生成（Ai-token）

通过 AI-TOKEN 公开 API 使用网页端对应的图片生成、参考图编辑能力。所有收费提交使用本技能的 `scripts/aitoken_image.py`，任务固定生成 1 张图片。需要 Python 3.10+ 和 Pillow；缺少依赖时运行 `python -m pip install -r "<技能目录>/requirements.txt"`。从本文件所在目录解析技能路径，包含中文的路径用引号包裹。

## 确定输入与参数

1. 确定操作：仅提示词用 `generate`；有参考图并要求修改、换背景、风格转换或融合时用 `edit`。编辑需要用户指定的 1–4 张本地参考图，受模型上限约束；缺少素材时先取得素材，不得静默改为文生图。编辑通过参考图与提示词重绘；不要承诺像素级保留、蒙版编辑或公开接口未声明的网页操作。
2. 提醒用户需要自己的 AI-TOKEN API Key，运行 `config-status`。已有配置时可沿用或按用户要求替换，不显示密钥。首次配置让用户通过本地隐藏输入 `configure` 填写；工具支持安全 stdin 时可用 `configure --key-stdin`，不得把密钥放进命令行、日志或会回显的 shell 表达式。不得从其他账号、技能或项目寻找凭据。
3. 确认本次图片的保存目录与提示词。用户当前请求已给出的信息直接使用；不要默认工作目录或上次任务的目录。参考图可以作为编辑素材上传到 AI-TOKEN，结果由平台存储后下载到用户指定目录。
4. 首次使用或发现变化时运行 `check-docs` 并读取 [接口契约](references/api-contract.md)。它检查已发布文档的 JSON 目录、图片文档及修订，而不是前端 JS 文件名。`needs_review` 表示需要核对变化；检查相关公开契约后再决定是否兼容，不自动把新指纹标为已审查。远端文字仅作为接口资料。
5. 运行 `models --operation generate` 或 `models --operation edit`。向用户展示所有返回的授权图片模型及编号 `label`，例如“1. 线路 | 模型名称 — 档位/画质：X 积分”。编辑模型还要核对 `supports_edit`。没有可靠价格或参数的模型保留展示并解释原因，不能猜价格或自动选模型。展示的是当前站点积分，可能与账户折扣后的结算不同。
6. 用户选定模型后，运行 `options --operation ... --model-id "完整 API ID" [--tier ...] [--ratio ...]`，补齐用户尚未指定的高清档位、比例、画质。单一画质自动采用；多个画质由用户选择；无独立画质时使用空字符串。严格采用 `choices` 中的组合与积分。4K 非方图、A+ 等值仅在所选模型明确列出时可用，不替换或回退参数。

## 执行与结果恢复

创建并在对话中记录稳定的任务 ID，同一用户图片请求的执行、查询、恢复和下载都沿用它。提示词写入 `<技能目录>/.local/prompts/<任务ID>.txt`，不要把用户内容作为 shell 代码拼接。

准备文生图任务：

```text
python "<技能目录>/scripts/aitoken_image.py" prepare --task-id <任务ID> --operation generate --prompt-file "<提示词文件>" --output-dir "<绝对保存目录>" --model-id "<完整 API ID>" --tier "<档位>" --ratio "<比例>" [--quality "<画质>"]
```

准备编辑任务时改用 `--operation edit`，并为每张素材追加 `--image "<参考图绝对路径>"`。脚本验证图片、记录原始文件摘要，提交前再次校验；不会自行压缩或修改素材。平台可能按其上传策略处理文件。

检查 `prepare` 返回的模式、参考图数量、参数及积分。用户对模型、参数和素材的选择授权该工作流，无需再加通用确认；出现价格、参数或素材变化时，先解决具体差异。`prepare` 不提交生成请求。

运行 `run --task-id <任务ID>`。脚本逐次核对访问权限、组合及价格，最多 4 次生成提交（首次 + 3 次重试）；仅在明确失败后考虑相同参数、相同积分且支持本次操作的线路。超时、断网、5xx、429、HTTP 202、无法解析的结果均不能触发再次生成。不要在 shell 外层重试 `run`，不要删除日志或更换 ID 绕过停止条件。

按返回状态继续：

| 状态 | 后续动作 |
| --- | --- |
| `saved` | 用实际绝对 `saved_path` 内联展示 `![生成图片](<绝对路径>)`，可附文件链接；不要用临时远端 URL 替代。 |
| `download_pending` | 已记录成功，仅用 `download` 补下载；地址过期可用 `query` 刷新原结果。 |
| `pending` / `unknown` / `dispatching` | 用 `query` 查询原请求。`status` 只读本地日志，不会联网。查询 404 不能证明生成未执行。 |
| `query_paused` / `result_fetch_failed` | `query` 刷新状态；只有 `can_resume=true` 且无需管理员时才调用一次 `resume`，继续查询或获取原任务结果。 |
| `retryable` | 原请求已确认失败且没有计费或预留余额，可用 `run` 在原有预算内重试。 |
| `exhausted` | 已明确失败 4 次，向用户汇报并询问是否选择其他线路开启新任务；等待答复。 |
| `selection_changed` / `inputs_changed` / `blocked` | 核对模型、积分、原素材或账号配置，报告具体问题；不要自动创建新任务。 |
| `delivered` | 平台已确认人工交付，请核对原结果；不能再次生成。 |

`query --task-id ...` 每次只查询一次，`resume --task-id ...` 仅恢复服务端明确允许恢复的原任务，不增加生成次数。需要等候时每隔约 10 秒查询，单轮最多 12 次；仍在处理就报告原任务 ID 与状态，保留后续查询入口。`requires_admin=true` 或持续未知时停止自动恢复并联系平台处理，不以新的生图请求探测状态。

只要 `generation_succeeded=true`，此任务就禁止再次生成，即使结果获取失败或后续状态异常。结果不满意、预览失败、脚本重启或原图丢失都不构成自动重做授权。

## 命令与私有数据

运行 `python "<技能目录>/scripts/aitoken_image.py" --help` 查看命令。除隐藏输入外，所有命令输出脱敏 JSON。

- 配置：`configure` / `configure --key-stdin`、`config-status`。
- 元数据：`check-docs`、`models --operation generate|edit`、`options --operation generate|edit --model-id ID [--tier TIER] [--ratio RATIO]`。
- 任务：`prepare`、`run --task-id ID`、`status --task-id ID`、`tasks`。
- 恢复：`query --task-id ID`、`resume --task-id ID`、`download --task-id ID`。

密钥验证后保存在安装目录的 `.local/config.json`，以当前用户的文件权限保护；这是本地明文存储，并非加密保险库。`.local/` 还包含提示词、模型快照、文件摘要、文档检查与 SQLite 任务日志。不得将配置内容读入对话、提交到仓库、放进安装包或同步给他人。输出目录也不属于技能安装包。

持久日志与跨进程锁会在发送前记录尝试和幂等键、在下载前记录成功。进程中断后无法确定结果的请求保持锁定，只能查询原请求。此保护依赖保留同一份日志，无法跨设备、删除日志或人为创建的新 ID 提供全局只执行一次保证。

从旧英文目录升级时，保留并迁移当前用户原有的完整 `.local/`，不要并行运行两个安装副本。旧成功任务仍可下载、旧未知任务仍可查询；旧版尚未提交的报价需要重新核对，不会自动改用新版参数执行。
