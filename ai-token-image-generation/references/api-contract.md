# AI-TOKEN 生图接口与更新检查

核对日期：2026-09-05。文档来源：[API 文档](https://www.ai-token.vip/dashboard/api-docs)。公共文档动态脚本也已读取；版本指纹保存在 `../config/api-contract.json`。下面记录的是契约摘要，不是用户配置。

## 认证与生成

- Base URL：`https://www.ai-token.vip/v1`。只把用户 Key 通过 `Authorization: Bearer` 发往该平台，不发到文档、图片存储地址或其他供应商。
- `GET /v1/models`：当前 Key 已授权可用的模型，`data[]` 包含 `id`、`channel_name`、`display_name`、`capabilities`；筛选 `image`，调用时保留精确 `id`。
- `POST /v1/images/generations`：`model`、`prompt`（1–20000 字符）、`n`（本技能固定 1）、`size`、`quality`、`response_format=url`。返回 `data[].url` 或 `data[].b64_json`。
- API 文档未列出价格、模型级参数枚举、图片任务查询接口或幂等保证。不要虚构这些契约。

## 实际可读取的公共元数据

- [模型市场](https://www.ai-token.vip/api/model-marketplace)：`models[]` 中用 `channel_name + display_name` 与授权模型精确匹配；`price_items` 是展示用档位价格，不能当作任意参数组合的准确报价。
- [生成参数选择器](https://www.ai-token.vip/api/content/ai-models/selector?section=generate)：`models[]` 的 `grsai_drawing_options` 提供 `image_size_options`、`aspect_ratio_options_by_size`、`quality_options`、`parameter_rows`；`parameter_rows[].site_price` 是对应 `tier/ratio/quality` 的站点积分。没有行定价时只接受 `pricing_source=site` 的明确档位定价，不能使用 `channel_pricing`。
- 参数选择器按网站功能可见性过滤，可能缺少仅开放 API 的模型。市场与选择器通过渠道、展示名称匹配，并在可用时核对 provider/integration/model 标识。缺失或重名歧义时列出“元数据不足”，不能猜模型或使用另一个渠道的价格。
- 价格在每次 POST 前重新读取并比较；它是发布的站点价格，不代表特定会员折扣的最终结算，也不能阻止服务器在最后一刻调价。

## 当前网关限制

公开文档只举例 `size` 和 `quality`。只读核对实现后确认：网关把 `quality=hd/high` 解释为 2K，其余解释为 1K；`size=1K/2K/4K` 覆盖档位且使用 1:1。`size` 可传比例字符串；图片质量的 low/medium/high 并未作为独立参数传给生成任务。

- 1K：`size=<ratio>`、`quality=standard`。
- 2K：`size=<ratio>`、`quality=hd`。
- 4K：仅可表达 1:1，`size=4K`、`quality=standard`。
- 固定画质模型：使用模型自身唯一画质。API `quality` 是档位映射，不能直接等同于选择器中的图片画质。
- 多画质模型、4K 非方图、A+：当前助手显示限制并拒绝该组合，不能偷偷传参并依赖服务器回退。只有官方契约/实现确认支持后才能更新映射。

这些实现观察不是已完成真实付费端到端验证的证明。迁移到其他版本时重新核对，不能自动把新版文档哈希当作已验证。

## 重试与不确定结果

- `n=1`；每个任务初次 + 最多 3 次重试，即第 4 次明确失败后停止并询问是否新选路线。
- 明确的 400/422 结构化拒绝可计为失败；身份/余额/额度错误立即停止。
- 超时、断网、5xx、429、无法解析的响应、缺少结果的 2xx、幂等键仍在处理中：无法排除服务端已执行，记为 `unknown`，禁止自动重发或换模型。
- 请求发送前将尝试号、幂等键与 dispatching 写入本地日志；每次确认失败后的新尝试使用独立幂等键。没有确认失败时不得产生新尝试。
- 成功先持久化原结果，再下载。下载或本地校验失败只补下载；成功记录不能改回 ready。
- 本地锁只保护同一安装目录。保持任务 ID 和日志，不能靠跨设备、换 ID、删除数据库绕过。

## 配置文件区别

- 公共契约：`config/api-contract.json`，记录文档 URL、日期、指纹和映射。
- 用户密钥：`.local/config.json`，仅保存在当前用户安装目录，隐藏输入并验证后写入，不输出 Key。
- 文档检查结果：`.local/docs-check.json`，记录观察到的指纹与差异，不覆盖公共已审查基线。
- 模型快照与任务日志同样在 `.local/`；发布包必须排除整个目录。
