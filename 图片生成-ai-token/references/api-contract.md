# AI-TOKEN 图片生成与编辑接口

核对日期：2026-10-28。依据：[线上接口文档](https://ai-token.vip/dashboard/api-docs)、[已发布 JSON 目录](https://ai-token.vip/api/api-docs)。本文仅包含可公开的调用契约；修订与指纹见 `../config/api-contract.json`。

## 认证、模型与参数

- Base URL：`https://ai-token.vip/v1`，Bearer API Key 仅发往本站需要鉴权的接口。图片下载与公共文档、公共参数查询不携带密钥；禁止跟随鉴权请求的重定向。
- `GET /v1/models` 返回当前 Key 可用的模型；筛选 `capabilities` 包含 `image`，保留完整 `id`。`office_image_contract.version=2` 且 `explicit_parameters=true` 才能使用显式图片参数；编辑还须 `supports_reference=true`，参考图数量受 `max_reference_images` 限制，本技能最多 4 张。
- [模型市场](https://ai-token.vip/api/model-marketplace) 通过 `channel_name + display_name` 精确匹配授权模型；[生图选择器](https://ai-token.vip/api/content/ai-models/selector?section=generate) 与 [编辑选择器](https://ai-token.vip/api/content/ai-models/selector?section=edit) 再按名称、provider、integration、model 标识精确匹配。缺失或重名时不能猜测。
- `grsai_drawing_options.parameter_rows` 的 `tier/ratio/quality/site_price` 给出有效组合及积分。没有行定价时仅采用 `pricing_source=site` 的明确组合或档位价格，不采用上游渠道成本。价格每次提交前刷新，仍不保证账户折扣或服务端最后一刻调价后的结算。
- 显式传递 `image_size`、`aspect_ratio`、`image_quality`；无独立画质时质量为空字符串。仅接受当前模型列出的组合，包括适用模型的 4K 非方图或 A+。不要再用旧 `size/quality` 的 1K/2K 映射推测参数。

## 创建图片请求

### 图片生成

`POST /v1/images/generations`，JSON：

```json
{
  "model": "当前授权模型的完整 ID",
  "prompt": "用户提示词",
  "n": 1,
  "image_size": "当前选择的档位",
  "aspect_ratio": "当前选择的比例",
  "image_quality": "当前选择的画质或空字符串",
  "response_format": "url",
  "result_delivery": "storage"
}
```

提示词为 1–20000 字符；本技能固定 `n=1`。每次实际尝试使用已写入本地日志的 `Idempotency-Key`。

### 图片编辑

`POST /v1/images/edits`，`multipart/form-data`。文本字段与生成相同，重复 `image` 文件字段发送 1–4 张参考图，由客户端生成 boundary。不能把参考图改成未声明的 JSON、远端 URL 或 `mask` 字段。

本地 helper 校验图片内容，支持 Pillow 可识别的 PNG、JPEG、WebP、GIF、BMP、TIFF、AVIF，单张本地上限 20 MiB；平台和模型可能有更严格限制。提交原文件字节，平台可按自身上传策略转换。编辑通过提示词和参考图重绘，不承诺网页专用工具的独立接口或像素级不变。

结果统一经平台存储，使用本站结果地址。本技能采用 `result_delivery=storage`，不依赖旧桌面直传约定。

## 异步结果、恢复与失败

- 成功响应的 `data[]` 包含 `url` 或 `b64_json`。先持久化结果，再下载、校验图片、保存原字节；下载失败不能重新生成。
- HTTP 202 表示尚未取得最终结果，可能包含 `status`、`tasks`、`requires_admin`、计费与预留余额。不能把它当作终止失败。
- `GET /v1/images/results?idempotency_key=...&endpoint=...`：使用创建时相同的 Key 与幂等键，`endpoint` 取 `/v1/images/generations` 或 `/v1/images/edits`。只查询原请求，不生成、不扣费。404 或查询不可用不构成重发依据。
- 返回 `tasks[]` 的 `task_id/status/can_resume`；仅在当前 `can_resume=true` 且无需管理员时，调用一次 `POST /v1/images/tasks/{task_id}/resume`。这是继续查询或重新获取原结果，不是重新生成。随后仍用 GET 获取最终结果。
- `pending/processing/queued/running`：继续有限次数查询；`query_paused`、`result_fetch_failed/result_save_failed`：按明确的恢复标志处理；`unknown/result_unknown` 或 `requires_admin=true`：保留原请求并核对。
- 结果获取失败可能发生在已成功、已计费之后。只要曾记录成功，或平台报告成功／结果获取失败，不能再允许生成请求；人工交付也不能重做。
- 明确的 400/422 结构化拒绝可记失败；401/402/403 立即停止。带有原任务或待处理信息的错误仍按未知处理。超时、断网、5xx、429、不可解析响应、无结果的 2xx 不重发。
- GET 明确返回 `failed`、计费和预留余额均为 0，且此前没有成功证据，才允许在原任务剩余预算内重试；同一次尝试只计一次失败。最多 4 次生成 POST，改模型必须保持同样参数、同样积分及编辑能力。

## 文档检查与升级

当前页面从 `/api/api-docs` 读取已发布快照；模型文档位于 `/api/api-docs/models/{文档ID}?revision={修订}`。`check-docs` 对目录和已审查的图片文档做规范 JSON 指纹检查，发现修订或内容变化时提示审查，不自动覆盖公开基线。价格与选项仍从当前模型接口刷新。

检查旧安装时保留 `.local/`，其中存有凭据与不可丢失的任务日志。旧日志缺少操作字段时按生成解释；已记录的幂等键保持不变。旧版未执行报价不会直接使用新版参数发送。

本契约已核对公开文档和实现，并有离线模拟验证；未使用真实 API Key 完成付费端到端验证。
