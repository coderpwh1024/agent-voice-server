# 语音输入确认与噪音处理

实时语音先经过 ASR 和输入检查，再提交聊天、记忆与工具执行。文字输入和独立听写保持原有语义：`input.text` 是用户明确发送的文字；`input_mode=transcription` 只返回草稿，不运行助手。

## 识别参数

`VOICE_VAD_THRESHOLD` 同时传入 realtime 协议的 `turn_detection.threshold` 和 Qwen Audio inference 协议的 `speech_noise_threshold`。Qwen Audio 3.1 默认使用近场 `near_meeting_16k`；远距离使用时可改为 `far_field_meeting_16k`。非 Qwen 模型不接收这些 Qwen 专用参数。

Nacos 配置可合并到现有文档，未填写的字段使用下列默认值：

```yaml
voice:
  vad:
    threshold: 0.2
    silence_ms: 500
    model: near_meeting_16k
  input_filter:
    min_speech_ms: 120
    min_active_ms: 80
    rms_dbfs: -55
```

未启用 Nacos 时，等价环境变量为 `VOICE_VAD_THRESHOLD`、`VOICE_VAD_SILENCE_MS`、`VOICE_ASR_VAD_MODEL`、`VOICE_INPUT_MIN_SPEECH_MS`、`VOICE_INPUT_MIN_ACTIVE_MS` 和 `VOICE_INPUT_RMS_DBFS`。Compose 同样传递这些变量。启用 Nacos 时，远端显式值仍优先于环境变量。

阈值越高通常越不容易把噪音当作人声，也可能漏掉轻声；每次调整应对比近讲、远讲、轻声、噪声和外放回声样本。[供应商参数说明](https://help.aliyun.com/en/model-studio/qwen-audio-asr-streaming-client-events)给出了两类 VAD 模型和噪声阈值的具体含义。

## 哪些输入不会自动提交

- 空白、纯标点、纯符号及已有少量中文语气词继续忽略。
- 中文或自动语言会话里的孤立短英文片段，例如 `Q.`、`KK.`、`XY`，以及连续重复的字母，进入待确认。常见 `OK/yes/no/hi` 等短回应、正常中文短指令、完整英文短语和混合语句正常处理；英文会话不因一个词较短就要求确认。
- 有逐句时间信息时，检查语音片段的长度和对应 PCM 的能量。持续时间不足，或有效能量不足的片段进入待确认。未收到可靠时间信息时不伪造声学证据，仍执行文字与回声检查。
- 转写与尚未播放完的助手音频文字相同或构成长片段匹配时，作为疑似回声进入待确认。短回应不会仅因与播报文字相同而拦截。

待确认不创建 `VoiceTurn`、不运行模型、记忆或工具，也不因该输入取消上一轮播报。用户可以继续说话，或编辑候选文字后显式发送。分类不是 KK/Q 黑名单；用户明确发送的英文、简称和字母不会被此流程过滤。

音频能量不等同于人声概率，近场 VAD 也不能排除所有误识别。没有原始问题音频时，自动化通过只能证明已覆盖的条件，不能声称真实环境不会再误触发。

## 增量事件与兼容

保持协议版本 `1`、PCM 帧格式、认证、线程所有权、输入幂等和取消语义不变。`/voice/capabilities` 和 `session.ready` 增加 `transcript_review: true`，支持该流程的客户端消费新增的 `transcript.review`。

待确认时，先发送同一 `input_id` 的空 `transcript.final`，清除临时字幕，再发送以下事件数据；实际事件还带有既有的 event、sequence、session 和 connection 标识：

```json
{
  "type": "transcript.review",
  "input_id": "connection:task:sentence",
  "text": "KK.",
  "reason": "uncertain_text"
}
```

`reason` 可为 `uncertain_text`、`insufficient_audio` 或 `possible_echo`。App 只展示候选卡片；“编辑这段文字”将内容追加至输入框，“忽略这段语音”丢弃候选。已有草稿不被覆盖，点击编辑本身不发送消息。最终仍通过 `input.text` 提交编辑后的文字。

旧客户端忽略新增事件，不会因待确认输入触发助手，但不能展示候选；可以重新说完整语句或手动输入。旧后端不具备服务端拦截能力，须与 App 一起升级。ASR 适配器中的逐句时间只用于服务器判断，不改变客户端音频帧。

## 诊断与验证

服务器保留最多 6000 个能量测量窗口，不保留原始 PCM。日志记录模型、分类、理由、字符数量、片段时长及有效能量时长，不记录转写正文。尚未确认的候选不进入历史和长期记忆。

定向回归：

```sh
APP_ENV=test uv run pytest tests/voice/test_input_filter.py tests/voice/test_realtime_service.py tests/voice/providers/test_alibaba_inference.py -q
```

测试覆盖可疑字母、纯符号、静音、短脉冲、播报回声、正常短中文/英文、时间边界、去重、手动发送与听写绕过、唤醒确认、配置校验及协议参数传递。Flutter 侧另覆盖候选编辑、草稿保护、结束收音、后台清理和录音权限等待期间的取消。

2026-11-16 使用当前 Nacos 模型配置做了三组合成素材的真实 ASR 验证：3 秒静音和三次短噪声脉冲均未返回最终转写；仓库合成语音 `with_command.wav` 返回包含预期指令的转写，并通过输入检查。验证使用新的近场 VAD 和噪声阈值，不调用聊天模型或 TTS，不创建数据库会话和聊天记录。它验证了参数可用及这三组样本的结果，不能替代真人、远场和实际手机回声测试。

本次完整回归：后端 1436 passed / 14 skipped；Flutter 221 passed / 1 skipped。Ruff 格式、lint、Pyrefly、Flutter analyze 与文档检查通过。已查看 320px、200% 字号、深浅色及键盘弹起时的候选卡片截图，编辑和忽略热区保持可见。

Android 正常入口 `lib/main.dart` 的调试 APK 已构建，构建号 103，默认服务地址 `http://127.0.0.1:8000`。产物位于相邻 App 仓库的 `build/app/outputs/flutter-apk/app-debug.apk`；本次没有覆盖安装手机、发布应用、修改 Nacos 远端文档或清理历史记录。手机原版本须更新后才能展示新的候选确认与收音交互。
