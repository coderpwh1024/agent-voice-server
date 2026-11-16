# Agent 评测工作流

当前可运行的是 **契约评测层**：真实业务编排配合 mock/fake 模型及隔离工具，固定选择高价值测试并阻断 socket 网络连接。它用于发现行为与安全边界回归，不产生真实 LLM 质量排名。

完整调研、代码映射、真实模型任务设计与分阶段方案见 [Agent 评测调研](research/2026-11-16-agent-evaluation.md)。

## 运行与报告

在仓库根目录使用已有开发环境：

```sh
uv sync --frozen
uv run --frozen python scripts/evaluate_agent.py --list
uv run --frozen python scripts/evaluate_agent.py
```

默认输出 `output/evaluations/contracts.json`，该目录已被 Git 忽略。评测需要开发依赖 pytest；不需要 Docker、Nacos 或真实模型凭据。评测器不加载生产应用生命周期，不发送邮件、不生成视频、不连接生产数据库。

按能力执行或指定报告路径：

```sh
uv run --frozen python scripts/evaluate_agent.py \
  --category memory --category authorization \
  --output output/evaluations/memory-safety.json
```

支持的分类为 `conversation`、`memory`、`email`、`video`、`maps`、`authorization`、`transport`、`voice`、`execution`。当前清单共 39 个场景；一个场景可能展开成多个参数化测试实例。

报告包含逐场景结果、每个测试实例的状态与耗时、pytest 退出码、错误类别、代码和测试源码摘要、依赖锁摘要、实际安装版本、Git 提交及工作区是否有修改。报告不保存捕获的 stdout、异常正文、提示词、工具载荷或对话内容；临时 JUnit 文件在读取状态后删除。测试 ID 可能包含合成参数，新增用例必须继续避免真实用户数据和秘密。

运行前后会核对应用源码、测试源码及协议快照、运行器与依赖锁的摘要。评测期间这些文件发生变化时，报告保留实例结果，但门禁失败并标记 `workspace_changed_during_run`；应在代码稳定后重新执行。

## 门禁和退出码

- `0`：选定场景全部执行且通过。
- `1`：测试失败、skip/xfail、setup/teardown 错误、缺失、超时或运行不完整。
- `2`：配置/路径/schema 错误，或基线不能比较。

`XPASS` 被强制视为失败，避免预期失败标记掩盖变化。环境中的 `PYTEST_ADDOPTS` 和配置中的 `addopts` 不会筛掉本次清单；运行器显式保留 `importlib` 导入模式。默认整次 pytest 超时为 300 秒，可用正整数 `--timeout` 调整。

本地或 CI 可以直接把退出码作为门禁。当前仓库没有新增 CI 平台配置；已有流水线只需调用相同命令并保留 JSON 报告。

评测插件在 pytest 配置阶段阻断 socket 连接、DNS 查询和追踪导出，以防选中测试误触真实网络。这是针对受控 Python 测试的防误调用措施，不是运行任意不可信代码的操作系统沙箱；清单与 pytest 插件仍属于可信仓库代码。

## 基线比较

先在需要比较的基准代码上保存报告，再运行候选：

```sh
uv run --frozen python scripts/evaluate_agent.py \
  --output output/evaluations/baseline.json

uv run --frozen python scripts/evaluate_agent.py \
  --baseline output/evaluations/baseline.json \
  --output output/evaluations/candidate.json
```

比较要求相同清单摘要、相同场景选择及相同参数化实例集合。报告列出从通过变为非通过的测试 ID；候选仍必须满足全部契约，基线中的失败不会获得豁免。基线存在基础设施错误或缺失实例时不能用于比较。

修改清单、重命名测试或改变参数化实例会使旧基线不兼容。应复核变化、在同一新清单下重跑基准与候选，不能删掉失败任务来制造提升。运行器拒绝把输出写到基线文件本身。

测试运行耗时用于发现测试开销变化，不能与真实模型首 Token、手机首响或线上 p95 混用。报告记录不同代码摘要正是为了说明候选与基线的差异来源；摘要相同也不保证外部环境完全相同。

## 定位失败和维护清单

终端打印分类通过数、失败/缺失测试 ID 和基础设施错误类别。报告不保存 traceback，需单独复现某个失败时可运行：

```sh
uv run --frozen pytest -p evaluations.offline \
  tests/agents/test_memory_commands.py::test_delete_requires_spoken_confirmation_and_uses_server_selected_id \
  -q
```

清单位于 `evaluations/agent_contracts.json`。每项包含稳定 `id`、`category`、可读 `title` 和函数级 `target`。目标使用 `tests/.../test_file.py::test_function`，不直接填写单个参数实例或测试类；函数的全部参数化实例自动进入评分。

新增场景前先明确可观察业务结果，优先复用或补充相邻域测试，再加入清单。清单校验拒绝重复 ID/目标和未知字段；结构测试检查所有目标确实存在。参数化实例必须全部通过，该场景才通过。

评测器本身的检查命令：

```sh
uv run --frozen pytest tests/evaluations/test_evaluate_agent.py -q
uv run --frozen ruff check scripts/evaluate_agent.py evaluations tests/evaluations
uv run --frozen ruff format --check scripts/evaluate_agent.py evaluations tests/evaluations
```

## 后续真实模型层

当前 CLI 只有 `contract` 模式，没有未实现的 `--live` 开关。下一层需保留真实的意图提取与主模型调用，只替换业务服务，并记录工具轨迹和最终业务状态。数据集需包含多轮输入、参考证据、允许/禁止动作和语义 rubric；不能把脚本化模型输出计算成真实模型识别率。

这类运行会产生模型费用，应在专用测试身份与预算内显式执行。独立保存模型、提示词/Skill 摘要、Nacos 版本、数据和评分器版本、缓存及超时配置。先用代码评分，再用人工校准的模型裁判；评分失败必须单独报告。Langfuse 可承接实验展示，但接入前应按仓库锁定 SDK 验证接口。

PostgreSQL、AG-UI 和 Langfuse 的真实集成仍由相应 `scripts/smoke_test.sh` 目标验证；当前契约套件通过不替代这些检查。真实 ASR/TTS、视频供应商结果和手机实际播放也需独立验证。
