# 语音图像编辑 LOOP

一张主图标识一次 LOOP。用户生成或打开本人图片，看到结果后说出具体改动。服务端保存一个独立版本，用户可以审阅、从旧版继续、选择定稿或主动结束。

## 入口、计数和出口

| 动作 | 判断与结果 |
| --- | --- |
| 生成初稿 | 写入 `image_generation_tasks`。图片转存成功后可启动 LOOP，已用次数为 0。 |
| 第一次编辑 | 语音明示要改的图片及内容，或通过 App 的 LOOP 编辑接口提交。根图和源图必须属于当前用户且已经保存。 |
| 继续或回到旧版 | 每次选择根图或本 LOOP 的任意可用版本，改动指令只来自当前请求。同一 LOOP 同一时刻最多有一个处理中版本。 |
| 查状态、澄清或重放 | 状态查询和澄清不消耗次数。相同提交键与参数返回同一任务。写入失败、队列满等入队前拒绝不消耗次数。 |
| 编辑入队 | 同一个 PostgreSQL 事务校验额度、分配 `version` 并写入版本记录。已入队任务无论成功、失败、受理状态不确定或之后被逻辑删除，都保留该次计数。 |
| 达到上限 | 第 5 次仍可完成和展示。处理结束后状态为 `EXHAUSTED`，用户可以查看和定稿；后续编辑在请求供应商前返回上限错误。 |
| 定稿或结束 | 「就用十三号图片」选择一张已保存的原图或版本并记录 `ACCEPTED`。「先不改了」结束当前图片 LOOP 并记录 `STOPPED`。已提交的任务照常处理。 |

示例：`12 → 13 → 14` 后回到 `12` 编辑得到 `15`。`15` 的父图为 `12`，计数为 `3/5`。同一个根图的旧版共用额度。定稿或停止后要开始新创作，先生成一张新图。

语音的「这张图」只解析同线程里有效的短期引用，保留十分钟。切换话题、看到多选结果或连接中断后可以报图片编号找回版本。编号、历史和 Nacos Skill 参考内容只辅助解析，操作授权来自本轮明确的请求和服务端所有权校验。

## Nacos 配置

将配置合并进 `agent-voice.yaml` 的图像业务分组。`image.loop` 管理额度，`skills.image` 管理语音 Skill，`models.dashscope.image` 管理供应商模型。

```yaml
image:
  loop:
    max_edits: 5
```

`max_edits` 默认 `5`，接受 `1` 到 `5` 的整数。变更后重启所有后端副本，使它们使用同一配置。调低上限时，已有版本和次数保留。`IMAGE_LOOP_MAX_EDITS` 可用于无 Nacos 的开发环境。应该先部署支持 `image.loop` 的代码，再把新键发布到对应环境。

使用匹配应用的 CLI profile、命名空间和分组。获取已有文档，保留其他配置，只增加上述分组或修改 `max_edits`。`config-get` 的交互输出带标题行，发布文件只放 YAML 正文。工作文件应保存在受保护的临时目录。

```sh
umask 077
loop_config_dir=$(mktemp -d)
nacos-cli --profile default config-get agent-voice.yaml DEFAULT_GROUP > "$loop_config_dir/current.txt"

# 提取 YAML 正文到 updated.yaml，合并 image.loop 后再发布。
nacos-cli --profile default config-set agent-voice.yaml DEFAULT_GROUP --file "$loop_config_dir/updated.yaml"
nacos-cli --profile default config-get agent-voice.yaml DEFAULT_GROUP > "$loop_config_dir/verified.txt"
```

配置正文可能包含凭据，自动化只输出目标字段的回读结果，验证后清理临时文件。应用启动的图像队列日志会打印 `loop_max_edits`。 [Nacos CLI 文档](https://nacos.io/en/docs/v3.2/manual/admin/nacos-cli/)

## PostgreSQL 表与部署

| 字段 | 含义 |
| --- | --- |
| `id` | 版本表自身的 BIGINT 自增主键。 |
| `image_id` | 对外图片编号，从主表的图片序列取号，一套编号可供语音查询和通用详情接口使用。 |
| `root_image_id` | 主图像表 ID，标识同一个 LOOP。 |
| `source_image_id` | 这一轮实际采用的原图编号，可以是根图或该 LOOP 的旧版。 |
| `version` | 同一根图下唯一的编辑尝试序号，范围 `1` 到 `5`。 |
| `title`、`image_url` | 本版本标题及转存成功后可用的七牛地址。 |
| `status`、`lease_version` 等 | 图片异步任务与租约恢复字段，与主表共用工作者。 |
| `loop_exit`、`accepted_image_id` | 最新轮次记录主动定稿或停止结果，定稿图可以是旧版。 |
| `create_by`、`update_by`、`create_time`、`update_time`、`is_delete` | 审计与逻辑删除字段。 |

通过 `image_work_queue` 视图，队列统一读取两张表的待处理任务，统一计算用户并发和供应商在途上限。任务各自回写所在表。历史主表图片继续按原编号访问，已有图片可作为新 LOOP 的根图。主图逻辑删除后其编号无法再开启或继续 LOOP，已有版本仍可由本人按编号查询。

应用启动会依次初始化主图表和 LOOP 表。在已经存在主图表的目标 PostgreSQL schema 中，也可以先单独应用脚本。脚本可以重复执行，保留现有记录。

```sh
psql --single-transaction --set=ON_ERROR_STOP=1 \
  --file=app/models/sql/create_image_edit_loop_versions.sql
```

部署时先执行增量 DDL，再更新后台工作者，最后发布语音 Skill 和 Nacos 配置。回退到只识别主表的服务前，应等待 LOOP 在途任务完成。保留两张表及其版本历史。

## App 和语音接入

| 接口 | 用途 |
| --- | --- |
| `GET /images/loops` | 分页查询当前 App 用户的 LOOP 历史，默认 `page=1&page_size=10`，`page_size` 可取 `1` 到 `100`。 |
| `GET /images/loops/{root_image_id}` | 使用历史记录的 `root_image_id` 查询同一条记录的最新详情。 |
| `GET /images/{image_id}/loop` | 用根图或版本编号取得 `root_image_id`、`used_edits`、`max_edits`、会话状态、定稿编号和版本列表。 |
| `POST /images/{image_id}/loop/edits` | 从指定根图或旧版本提交编辑，必传 `Idempotency-Key`。返回 `202` 和版本图片。 |
| `POST /images/{image_id}/loop/close` | `{"action":"ACCEPTED"}` 采用 URL 中的已保存图片。`{"action":"STOPPED"}` 停止该图所在 LOOP。 |
| `GET /images/{image_id}` | 查询单张主图或 LOOP 版本，可沿用现有查看、改名和删除入口。 |

历史接口使用 App 用户 Bearer 令牌，沿用 `{"code":200,"message":"success","data":...}`。列表的 `data` 包含 `items`、`total`、`page`、`page_size`、`total_pages`，按首次编辑入队时间倒序，同一时间按根图编号倒序。第一页之外无数据时 `items` 为空，`total` 仍返回符合条件的记录数。App 可以使用 `GET /images/loops?page=2&page_size=10` 翻页，并使用列表项的 `root_image_id` 查询详情。

列表项和详情的 `data` 使用同一个结构。

| 字段 | App 用途 |
| --- | --- |
| `root_image_id`、`root_image` | LOOP 的稳定编号和完整原图。 |
| `versions` | 按 `loop_version` 升序排列的可见编辑记录，包含 `source_image_id`、标题、提示词、模型、尺寸、参考文件编号、任务状态、错误码、七牛地址和任务时间。处理中和失败的版本也会返回，未转存成功时 `image_url` 为 `null`。 |
| `status`、`used_edits`、`max_edits` | LOOP 当前状态和额度。已删除的轮次仍计入 `used_edits`。 |
| `accepted_image_id` | 用户定稿时选择的图片编号，未定稿时为 `null`。 |
| `preview_image_id`、`preview_image_url` | 卡片预览优先使用仍可见的定稿图，其次使用最近一次转存成功的可见版本，最后使用根图。 |
| `create_time`、`update_time` | 首次编辑入队时间、根图与版本中最近一次数据库更新时间。 |

已提交至少一次 LOOP 编辑的根图进入历史，语音和 App 提交的编辑都使用同一份记录。逻辑删除根图会隐藏整条历史，逻辑删除版本会隐藏对应的 `versions` 项。根图没有 LOOP 编辑、已删除、编号不存在或不属于当前用户时，历史详情返回 `404`。

`GET /images` 和语音按标题、描述搜索返回主表作品。普通 `POST /images/{image_id}/edits` 在源图已经是 LOOP 根图或 LOOP 版本时共用该会话的配额。要从尚未开启 LOOP 的主图明确启用该功能，使用 `/loop/edits`。

语音 `image.task` 和执行进度的版本卡携带可选 `loop_root_image_id` 和 `loop_version`。关闭后发送 `image.loop`，包含会话状态和定稿编号。客户端可用根图编号拉取版本列表，在第五版提交后保留「查看版本」与「采用」操作。

## 验证与排障

```sh
uv run pytest tests/agents/test_image_voice.py tests/api/test_image.py tests/api/test_nacos.py -q
uv run pytest tests/smoke/test_image_postgres.py --run-docker -q
```

PostgreSQL 用例检查从旧版继续、并发争用第 5 次、请求重放、版本只入独立表、配额保留、跨账号隔离、停止后的任务恢复，以及历史分页、预览选择和逻辑删除。该测试使用独立 schema，百炼和七牛由测试替身提供。

服务日志按根图、版本号、任务编号和错误码定位入队、上限、主动退出和任务失败。语音原文、编辑指令和含签名地址不进入这些日志。遇到 `SUBMISSION_UNKNOWN` 先核对原任务再明确选择下一步，后台不会重提该供应商请求。
