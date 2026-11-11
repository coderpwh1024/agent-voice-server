# 手机端 Markdown 记忆

手机端 `MEMORY.md` 保存本机记忆正文。服务端保留现有 `mem0-chatbot` Agent、Nacos 聊天提示词、PostgreSQL 检查点及全部 Mem0 代码/API。本机模式和 Mem0 模式分别管理，不自动合并或迁移旧记忆。

## 使用

升级后端与 App 后，从“记忆与对话 → 本机记忆”或个人主页进入。兼容后端默认使用本机模式，可以在本机记忆页选择下次连接使用 Mem0 备选。已经连接的会话保持创建时的模式，切换后重新连接生效。

页面支持手动添加、分类/搜索、编辑、查看原文证据、忘记、清空、自动整理开关、回答使用开关、候选预览与确认。Markdown 备份通过明确确认后复制到剪贴板；导入时粘贴完整备份并确认替换。本期没有自动云备份或跨设备同步。

Android 使用 `noBackupFilesDir/memories/<账户作用域>/MEMORY.md`；iOS 使用排除系统备份的 Application Support 目录，并设置系统文件保护。作用域同时包含服务地址与用户 ID。文件包含来源片段、候选和待整理任务，属于私人数据；未实现独立的应用层加密保险库。卸载或丢失手机可能丢失文件，应主动导出到安全位置。导出会进入系统剪贴板，使用后应及时清理。

## 数据流

1. 服务端已接受的最终文字或 ASR 输入通过 `input.accepted` 带回稳定 `turn_id`。控制确认等合成输入标记为不适合提炼；中间识别不进入本机任务。
2. App 将来源与任务写入本机 MD，后台调用候选提炼接口。重启后再次连接或在记忆页重试，可继续未完成任务；不会依赖手机后台常驻。
3. 每轮生成前，服务端通过 `memory.context.request` 请求相关参考，包含 `response_id/source_id/thread_id/text`。
4. App 用核心画像与中文双字词/英文词的轻量本地匹配筛选至多 8 条、总计 3000 Unicode 码点的完整记忆，通过 `memory.context` 返回。当前未实现手机端向量语义检索。
5. 后端将参考置于独立的运行作用域，并以不可信参考交给模型；不会把参考原文写入 LangGraph 状态或配置，也不会在本机模式调用 Mem0 搜索、写入或管理操作。
6. `POST /local-memories/extract` 返回结构化候选，不代表手机已经保存。低敏新增候选可在本机自动写回；其他新增、修改、删除、清空等待 App 确认。服务端没有直接读写手机文件的工具，主 Agent 会提示在本机记忆页处理管理操作。

接收到本机参考有 3 秒默认预算。超时发出 `memory.context.unavailable`，继续回答并在 App 提示缺少个人上下文，不自动读取 Mem0 替代。候选提炼失败保留本机待整理任务，提供重试；不会让语音回答等待提炼。

## 接口

本机提炼接口位于现有 bearer 保护之下。普通用户不能通过 `user_id` 冒充他人；可信服务凭据必须显式指定作用域。

```http
POST /local-memories/extract?user_id=42
Authorization: Bearer <app-user-token>
Content-Type: application/json
```

```json
{
  "source_id": "f1285ba6-0b24-4a50-adc3-c858eb932338",
  "text": "请记住，我希望回答尽量简短。",
  "occurred_at": "2026-11-11T08:30:00Z",
  "utc_offset_minutes": 480,
  "context": {
    "revision": 4,
    "epoch": 1,
    "total_count": 0,
    "memories": []
  }
}
```

响应 `data` 包含原 `source_id/revision/epoch`、`status=proposed|no_change` 与 `proposals`。每个候选有 `action`、`category`、`text`、`evidence`、可空 `target_id`、`requires_confirmation`。证据必须在本轮输入中实际出现；目标必须来自上传的参考；修改、删除和清空强制等待确认。凭据形态与显式“这句别记”会在模型调用前过滤。模型提炼仍可能误判，确认与纠错入口应保留。

App 在创建本机任务时记录输入时间和时区偏移，离线重试继续使用原时间，供模型理解“今天”“下周”等表达；条目保留来源时间。旧调用省略时间字段时使用服务端接收时间与 UTC，因此需要相对日期的客户端应显式传入。

HTTP invoke/stream 可通过顶层 `local_memory` 传相同上下文；AG-UI 使用 `forwardedProps.local_memory`。相关内容不会通过 `agent_config` 或 `forwardedProps.configurable` 传递，这些名称属于保留键。

语音创建请求新增可选 `memory_mode=local|mem0`，省略时保持旧 Mem0 行为。`/voice/capabilities` 的 `local_memory` 字段用于协商版本和预算；旧客户端不用理解新事件。PCM 帧格式与语音协议主版本保持不变。

## 文件、一致性与遗忘

MD 文件由 JSON 兼容 YAML 的 frontmatter 与可读正文组成。frontmatter 保存格式版本、修订、遗忘代次、开关、条目元数据和耐久任务；正文以 `## memory:<UUID>` 分段保存记忆文本。正文是恢复时读取的内容，支持通过备份编辑正文再导入。拒绝重复编号、超限文本、损坏格式和不匹配的目录；不会把损坏文件静默替换为空库。

同账户通过共享 store 串行执行写入，先写 `.next` 并 flush，再 rename 替换；写入失败恢复内存状态。该机制面向单 App 进程，不支持多个设备或多个独立进程直接共享一个文件。

编辑、删除、清空、导入、切换模式或回答使用开关会使旧提炼任务/候选失效，迟到的网络结果不能写回。显式变更按条目版本检查，候选按修订与遗忘代次检查；冲突时需要重新整理或手动修改。本期采用保守策略，一次已确认变更会取消其余待处理候选与任务。

删除仅处理本机记忆，服务端聊天与 Mem0 备选历史仍有独立管理方式。为避免旧个人事实通过近期对话重新进入模型，本机遗忘代次变化后，该线程的普通聊天上下文从当前轮重新开始；历史展示仍保留。以后用户重新明确提到某项信息，可形成新的来源。此行为不承诺删除模型供应商或外部追踪系统中的既有数据。

## 设置与日志

| 环境变量 | 默认值 | Nacos 对应项 |
| --- | --- | --- |
| `LOCAL_MEMORY_ENABLED` | `true` | `memory.local.enabled` |
| `LOCAL_MEMORY_EXTRACTION_TIMEOUT_SECONDS` | `20` | `memory.local.extraction_timeout_seconds` |
| `LOCAL_MEMORY_CONTEXT_TIMEOUT_SECONDS` | `3` | `memory.local.context_timeout_seconds` |

`LOCAL_MEMORY_ENABLED` 与原有 `MEM0_ENABLED` 独立控制：本机模式无需启用 Mem0；需要提供 Mem0 备选时再按原配置启用。聊天提示词、认证、PostgreSQL 检查点和语音供应商仍遵循项目原有配置。升级 App 与后端后重新连接，才能协商本机协议。

后端记录候选提炼开始、完成、失败/超时，以及每轮参考接收、条目数量、修订、代次和耗时。记录 `source_id/response_id` 方便关联，不输出记忆、原输入、原音频、模型原始结果或密钥。服务端提炼完成日志只代表返回候选，不能当作手机落盘确认。

本地正文仍可能通过选中的参考、提炼输入或模型生成的回答离开手机；聊天历史与可选 LangFuse/供应商处理遵循现有配置。不能将“MD 在手机”表述成“所有信息永不离开设备”。

## 验证范围

后端测试覆盖认证/用户绑定、预算、证据和目标校验、空结果、失败、日志脱敏、Mem0 隔离、上下文清理、遗忘代次和 WebSocket 参考往返。前端测试覆盖文件恢复、中文匹配、Unicode 预算、任务重试、幂等、版本冲突、删除防复活、开关、导入校验、协议往返，以及小屏大字体页面。

真实模型提炼质量、真实语音链路及新安装包的设备验收需单独执行，不能由模拟模型测试或构建成功替代。
