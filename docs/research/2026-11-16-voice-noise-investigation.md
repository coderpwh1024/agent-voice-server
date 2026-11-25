# KK、Q 异常语音输入排查

排查日期：2026-11-16。记录时间均为 Asia/Shanghai（UTC+8）。

本文保留修复前的调查事实。后续代码已接入输入确认、声学检查与前端收音清理，当前行为与配置见[语音输入确认与噪音处理](../voice-input-filter.md)。

## 结论与边界

实际出现的 `KK.`、`Q.` 来自同一次唤醒后持续对话中的 ASR 转写，进入后端后被接受为新输入，并触发了模型回答。它们不是通过 App 的文字发送事件提交的，也不是单纯的前端展示异常。

已经确认两处防护缺口：当前 inference 协议未接入噪声识别阈值与近场 VAD 配置；前后端只过滤空白、标点和少量中文语气词，异常英文短词会继续进入聊天流程。

没有证据能进一步确定当时的声源是手指摩擦、环境声音还是扬声器回声。当前持久化记录没有原始音频、逐句声学指标和触摸事件时间线，不能把声源猜测当作已确认根因。本次只做读取、离线复现和调查记录，没有修改运行时行为、远端配置或历史数据。

## 实际数据

对 PostgreSQL 使用只读事务与查询超时，输出只包含相关短词、时间和汇总，没有展开无关聊天正文、用户标识或连接凭据。

库内语音轮次覆盖 2026-09-18 至 2026-11-16，共 2930 轮、221 个会话。以下短词统计限定最近 30 天，去除空白及标点后筛选 1–4 个 ASCII 字母，不代表这些输入全部都是噪音。

| 内容 | 出现次数 | 输入来源 |
| --- | --- | --- |
| T | 7 | ASR |
| C | 1 | ASR |
| KK | 1 | ASR |
| Q | 1 | ASR |
| SI | 1 | ASR |

目标事件位于同一会话、同一线程：

| 时间 | 事件 | 证据 |
| --- | --- | --- |
| 17:07:34 | 创建会话 | `activation=wake_word`、`input_mode=conversation`、`turn_detection=server_vad` |
| 17:07:37 | 唤醒确认后问候 | 唤醒状态为 accepted |
| 17:07:44 | 先前正常长度的语音输入 | 正文未展开 |
| 17:08:17 | 收到 `KK.` | ASR 输入标识，模型已生成 51 字回复 |
| 17:08:28 | 收到 `Q.` | ASR 输入标识，模型已生成 48 字回复 |

两条目标输入的标识均符合后端的 `connection_id:task_id:sentence_id` 结构，进一步佐证实际经过 inference ASR 适配器，而不是客户端 `input.text` 的随机事件 ID。两轮执行状态均为 completed，音频状态最终为 cancelled；不能把最终 cancelled 理解为没有触发模型。

同会话客户端上报 10 次音频统计、1250 帧，`aec_enabled=true`、`noise_suppression_enabled=true`，削波样本为 0。标志只说明客户端曾上报相关处理启用，不证明每一句都没有残余噪音或回声。会话级聚合指标也不能还原目标短词发生时的信噪比。

## 前端按键与输入链路

排查范围为相邻 `agent-voice-app` 仓库。连接手机已安装版本为 1.0.0、构建号 102，最近安装时间为当天 16:10，USB 已存在本地 8000 端口映射；本次没有覆盖安装、清除数据或在真实会话中提交消息。

| 控件 | 当前行为 | 与异常短词的关系 |
| --- | --- | --- |
| “现在开始说话”大按钮 | 单击进入持续语音对话，再次操作结束 | 误点可能开启收音；目标会话由唤醒词启动，不能归因为该按钮启动 |
| 输入框左侧“语音转文字” | 单击开始听写，再次单击完成，结果填入草稿 | 即使转写出短词，也要另点发送才能提交 |
| “我要插话” | 发送 `input.speech_hint`，临时暂停播报 | 不构造文字输入 |
| 文字发送按钮或键盘发送 | 提交当前非空草稿 | 目标两条记录没有走此来源 |

这些控件不是按下即提交文字的 `onPointerDown` 或 `onTapDown` 实现。补充的离线 Widget 检查确认：

1. 从小麦克风按下后拖动滑走，不启动听写，也不发送文字。
2. 听写返回 `KK. Q.` 时停留在草稿，只有显式点击发送才提交。
3. 在 320 和 390 逻辑像素宽、200% 字号、正在播报的状态下，听写、插话、停止、发送四个按钮均至少 48×48，热区不重叠。

存在交互风险：听写与插话使用相同的麦克风图形，正常界面主要依靠边框和 Tooltip 区分。误点听写会切换模式并结束原持续对话，但不能据此认定它导致了本次两条记录。设备当前系统触摸音效开关为关闭；这不等于证明历史事件发生时不存在摩擦声、键盘声音或其他声音。

持续对话中，`_handleMicrophone()` 会上传采集到的音频；RMS 逻辑主要用于播报中的插话提示，不是上传前的人声过滤器。输入框获得焦点也不会自动暂停已有持续对话。因此，输入、触摸和助手播报期间仍可能收音。

相关位置：

- `../agent-voice-app/lib/features/voice_session/voice_home_page.dart`：`_VoiceControls`、`_Composer`、`_toggleDictation()`。
- `../agent-voice-app/lib/features/voice_session/voice_session_controller.dart`：`_handleMicrophone()`、`_maybeInterrupt()`、`transcript.final` 事件处理。
- `../agent-voice-app/android/app/src/main/kotlin/com/coderpwh/agent_voice_app/MainActivity.kt`：`startCapture()` 的 Android AEC 与 NoiseSuppressor 初始化。

## 后端噪音处理缺口

### 当前模型没有收到已有噪声阈值

Nacos 当前配置的 ASR 为 `qwen-audio-3.1-asr-flash-streaming`，自动选择 inference 协议。配置中的 VAD 值为 `threshold=0.2`、`silence_ms=500`。

`app/services/voice/providers/alibaba_inference.py` 的 `InferenceStream.start()` 当前发送：

```json
{
  "format": "pcm",
  "sample_rate": 16000,
  "max_sentence_silence": 500,
  "language_hints": ["zh"]
}
```

离线截获请求确认：把 `VOICE_VAD_THRESHOLD` 从 0.2 改到 0.8，上述参数完全相同。该设置目前只在 `app/services/voice/providers/alibaba_realtime.py` 的 realtime 分支传递。

当前模型支持 inference 专用的 `speech_noise_threshold`，并支持用 `vad_model` 选择近场或远场；未指定时使用远场 `far_field_meeting_16k`。手机近距离使用应评估 `near_meeting_16k`，而不是仅调整未进入此协议的 realtime 参数。提高噪声阈值也可能漏掉轻声，必须用真实场景对照验证。参数语义来自[阿里云官方客户端事件文档](https://help.aliyun.com/en/model-studio/qwen-audio-asr-streaming-client-events)。

### 异常短词直接变成正式消息

`app/api/voice_connection.py` 的 `is_non_actionable_voice_text()` 只拦截空白、标点及 `嗯/嗯嗯/呃/额/啊/呢/那`。ASR 最终结果通过后，会先发出 `transcript.final`，再调用 `submit()`。

离线调用真实识别处理与提交逻辑，输入模拟的上游最终转写：

| 模拟最终转写 | 手动开启的持续对话 | 已确认唤醒的持续对话 |
| --- | --- | --- |
| `KK.`、`Q.`、`T.`、`C.` | 创建新轮次 | 创建新轮次 |
| `嗯。`、`。` | 忽略 | 忽略 |
| `好的`、`停` | 创建新轮次 | 创建新轮次 |

这项复现证明转写后的防护缺口，不是实际噪声到 ASR 输出的声学复现。`submit()` 接受新输入时还会尝试取消尚未播完的上一轮回答，因此误识别可以同时造成聊天污染和播报被打断。

前端采用相同的少量语气词规则。收到这些短词的 `transcript.final` 后，会直接追加 human 消息。这解释了短词为什么出现在对话框里；只在前端隐藏短词不会阻止后端已经开始的执行。

## 修复优先级

1. 在当前 inference 路径接通可校验、可配置的 `vad_model` 和 `speech_noise_threshold`，同步 Settings、Nacos 映射、示例与供应商请求测试；按手机近讲、远讲、轻声、环境噪声分别校准。
2. 在后端发出正式转写和调用 `submit()` 之前处理可疑语音。结合逐句时长、声学证据和会话上下文判断，避免只把 KK/Q 加入黑名单，也避免误伤“停”“好的”和真实英文内容。显式文字发送、听写草稿继续保留用户原意。
3. 增加最小化诊断信息：输入来源、ASR 模型与协议、逐句时间范围、过滤决定与理由、关联音频指标；不默认记录原始音频或无关聊天正文。
4. 区分听写与插话的可见文案或图标，明确持续收音状态，并提供容易到达的暂停收音操作。是否在输入时自动暂停属于产品行为选择，应单独验证。

## 验证结果

- 后端：`APP_ENV=test uv run pytest tests/voice/test_realtime_service.py tests/voice/providers/test_alibaba_realtime.py tests/voice/providers/test_alibaba_inference.py -q`，129 passed，3 项现有依赖/序列化警告。
- 前端：语音首页、会话控制器、文字/听写控制器三个测试文件，28 passed。
- 补充 Widget 调查：4 passed，覆盖拖动取消、异常短词听写草稿与显式发送、两个宽度下的热区尺寸及重叠检查。调查脚本位于系统临时目录，没有改动前端仓库。
- 离线后端调查：16 个转写/激活组合及 2 个 VAD 阈值请求截获，结果如上；没有调用真实 ASR、LLM 或 TTS，不产生模型费用。
- 未执行实际噪声录制及付费模型对照；没有修改持久化或服务生命周期，不需要基础设施冒烟测试。本次没有修复代码，因此相关测试通过不代表误识别问题已经修复。

调查状态：输入来源与转写后放行链路已确认；声源与声学参数效果仍待受控验证。
