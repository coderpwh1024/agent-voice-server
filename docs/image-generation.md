# 千问图像生成与编辑

图像工作室支持文生图、上传参考图后图生图、从已保存的作品继续编辑，以及查看、改名和逻辑删除。每个任务生成一张图片。编辑创建独立作品并记录父图编号。生成结果转存到七牛后才向客户端提供地址。

## 部署

将以下片段合并到 Nacos 主配置 `agent-voice.yaml`。`api_base_url` 使用与 API Key 同地域、同业务空间的百炼地址。专用 Key 可放在 `models.dashscope.image.api_key`，省略时复用 `models.dashscope.api_key`。上传和结果转存共用现有 `storage.qiniu` 配置，`storage.files.enabled` 控制参考图上传。[百炼图像异步 API](https://docs.bailian.console.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference)

```yaml
models:
  dashscope:
    image:
      api_base_url: https://workspace-example.cn-beijing.maas.aliyuncs.com/api/v1
      model: qwen-image-3.0
image:
  enabled: true
  max_active_tasks: 4
  max_active_tasks_per_user: 2
storage:
  files:
    enabled: true
```

配置更新后重启服务。启动时检查应用令牌密钥、百炼模型入口、七牛配置并初始化队列表。所有副本应加载同一套配置。修改百炼业务空间、地域或 Key 前应等待现有任务结束，以便继续按原供应商身份查询。通过 [`.env.example`](../.env.example) 中的 `IMAGE_*` 变量也可配置功能。

可在重启前用目标 PostgreSQL 连接依次执行主表和 LOOP 脚本。脚本在目标 schema 中创建所需对象，支持重复执行。

```sh
psql --single-transaction --set=ON_ERROR_STOP=1 \
  --file=app/models/sql/create_image_generation_tasks.sql \
  --file=app/models/sql/create_image_edit_loop_versions.sql
```

## REST API

接口使用 App 用户 Bearer 令牌。成功响应沿用 `{"code":200,"message":"success","data":...}`，创建与编辑的 HTTP 状态为 `202`。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /files` | 用 `multipart/form-data` 的 `file` 字段上传参考图，响应 `data` 含 `id` 和七牛 `url` |
| `POST /images` | 生成图片，支持 `prompt`、`title`、`size`、`source_file_ids` |
| `POST /images/{image_id}/edits` | 使用已保存作品作为首张参考图，按 `prompt` 生成新作品 |
| `GET /images` | 分页查看本人作品，参数 `page`、`page_size`、`status`、`q`、`search_field`。`q` 在标题和提示词中做大小写不敏感的子串匹配，设 `search_field=title` 或 `content` 可限定字段 |
| `GET /images/loops` | 分页查看本人的 LOOP 创作历史，默认每页 10 条 |
| `GET /images/loops/{root_image_id}` | 查看某次 LOOP 的原图、编辑版本、预览和定稿状态 |
| `GET /images/{image_id}` | 获取任务状态及保存后的 `image_url` |
| `PATCH /images/{image_id}` | 更新展示标题，请求体为 `{"title":"新标题"}` |
| `DELETE /images/{image_id}` | 逻辑删除已结束任务，返回 `{"deleted":true}` |

`POST /images` 和 `POST /images/{image_id}/edits` 必须提供 `Idempotency-Key`。同一用户用相同键及相同请求重试时取得原任务，改动参数会收到 `409`。提示词限 4000 字符，标题限 200 字符。`size` 可省略，指定时用 `1024*1024` 格式，像素面积需在 512² 与 2048² 之间，宽高比在 1:8 与 8:1 之间。

上传图片后，App 用返回的 `url` 预览，用 `id` 作为 `source_file_ids` 元素提交。服务端从上传记录验证归属及大小，再向百炼传递已保存的地址。独立生成最多接收三张上传图。编辑已有作品时，可额外附加两张上传图。每张参考图最大 10 MiB，支持 JPEG、PNG、BMP、TIFF、WebP 和 GIF。

任务的公开字段包括 `id`、`title`、`prompt`、`model`、`status`、`source_image_id`、`source_file_ids`、`size`、`image_url`、`error_code`、`create_time`、`update_time`。列表的 `data` 含 `items`、`total`、`page`、`page_size`、`total_pages`。

不存在或不属于本人的作品和素材返回 `404`。原图还未生成成功、删除目标还在处理或个人队列已满时返回 `409`。参数校验错误返回 `422`。功能关闭或任务存储不可用返回 `503`。

## 异步队列、归档与恢复

1. `QUEUED` 表示任务已持久化。后台工作者从 PostgreSQL 领取任务并落盘 `SUBMITTING`，随后调用百炼异步生成接口。百炼受理后保存 `task_id`，按 `PENDING` / `RUNNING` 轮询。数据库控制所有副本的供应商在途任务上限，并限制每用户尚未结束的任务数。
2. 百炼返回临时图片后，后台进入 `STORING`。优先下载并验证 PNG，再保存到固定七牛对象键 `images/qwen/{task_id}.png`；下载失败时尝试由七牛从阿里云 OSS 抓取。重试可通过七牛对象元数据确认此前已完成的写入。只有七牛确认后，任务才进入 `SUCCEEDED` 并清除临时 URL。[七牛第三方资源抓取](https://developer.qiniu.com/kodo/api/fetch)
3. 工作进度、下一次处理时间和处理租约均保存在数据库。服务重启后工作者继续处理到期任务。前端可每五秒查询未完成的任务，主动查询会唤醒后台检查，但复用现有轮询频率和租约。短时供应商或转存错误使用退避重试，供应商阶段的保守截止时间为 23 小时。已知供应商任务可续查，暂存文件可续传。
4. 如果提交请求可能已被百炼受理但服务端没有确认任务 ID，例如提交期间断线或进程退出，任务以 `SUBMISSION_UNKNOWN` 结束。后台不会再次提交同一请求。可先在百炼控制台核对计费或任务记录，再决定是否从工作室新建任务。其余终态为 `SUCCEEDED`、`FAILED` 和 `CANCELED`。

图像任务的删除为逻辑删除。删除后，任务从本人的列表和详情中消失。已经发布的七牛地址可能继续访问，父子版关系及底层对象保留。服务日志以任务编号和供应商请求 ID 关联处理阶段、状态和错误类型，省略提示词、签名 URL 和密钥。

语音每轮最多提交一次图像操作。语音 LOOP 版本存入独立表，同一个根图最多五次编辑。App 通过独立接口查看版本、继续编辑及定稿，见[语音图像编辑 LOOP](image-loop.md)。

## 验证

```sh
uv run pytest tests/api/test_image.py tests/services/test_image_providers.py -q
./scripts/smoke_test.sh postgres
```

有独立测试 PostgreSQL 时，可设置 `POSTGRES_*` 后运行 `uv run pytest tests/smoke/test_image_postgres.py --run-docker -q`。测试在唯一 schema 中验证幂等、授权、全局并发、租约恢复、七牛临时失败后的重试和语音编辑上限。真实联调时，用图像工作室提交一条生成及一条编辑请求，在任务到达 `SUCCEEDED` 后确认图片可由预期设备访问。

2026-11-22 本地联调已执行应用库 DDL，通过 Nacos CLI 发布并回读图像配置，重启后确认健康检查和图像后台任务启动。使用隔离身份验证了真实七牛素材上传、百炼异步生成、七牛 PNG 读取、改名、查询、跨账号隔离和逻辑删除。合成普通话经真实 ASR 分别提交了生成及编辑，两个任务均转存成功。Android 真机通过 USB 本地地址显示了实际成图，正常入口安装包已覆盖安装并启动。
