# 语音生成与编辑图像

登录 App 后，在对话模式可以说「生成一张纸艺图像，画面里有一座绿色小山」。语音会播报图像编号，图像任务同时出现在图像工作室。可以说「查一下 12 号图片的状态」。等待图片完成后，在同一对话说「把这张图的背景换成蓝色」，也可以直接说图片编号。编辑会生成有独立编号的新版本。

## 在 Nacos 管理 Skill

`manage-user-images` 位于 Nacos 控制台的「AI 注册中心 → Skill 管理」。仓库中的 [SKILL.md](../skills/manage-user-images/SKILL.md) 是发布源文件，包含生成、编辑、查询、追问和分步编辑的工作流。服务启动时下载配置指定的已发布版本，只有识别到语音图像请求时才将说明作为参考交给对话模型。鉴权、操作参数、幂等、并发与编辑上限由服务端执行。

发布或更新时，先上传，再提交审核。查看版本状态后，若版本已经 `online`，即可使用。若已通过审核且等待发布，再执行 `skill-release`。将下例的 `1.0.0` 改为本次实际上传版本。 [Nacos CLI 文档](https://nacos.io/en/docs/v3.2/manual/admin/nacos-cli/)

```sh
nacos-cli skill-upload ./skills/manage-user-images
nacos-cli skill-review manage-user-images --version 1.0.0
nacos-cli skill-describe manage-user-images
nacos-cli skill-release manage-user-images --version 1.0.0
```

将以下片段合并到已有 `agent-service-toolkit.yaml`。确认 `image.enabled` 已启用，发布配置后重启后端。`version` 优先于 `label`，省略 `version` 时默认使用 `latest`。

```yaml
skills:
  image:
    enabled: true
    version: '1.0.0'
    timeout_seconds: 20
```

服务启动日志记录加载的 Skill 名称、版本或标签和内容摘要。启用时需要有效的已发布 Skill。关闭 `skills.image.enabled` 可暂停语音图像操作，App 图像工作室仍由 `image.enabled` 控制。

## 逐轮编辑 LOOP

1. 入口是经过认证和线程校验的语音对话。当前话语给出明确指令。用户可指定本人作品编号，或明确指代同一语音线程中最近操作的作品。
2. 每轮最多解析一次模型意图并提交一个异步图片任务。多项可以同时完成的改动可合成一次编辑。需要依次完成的改动，由用户等当前版本保存后发起下一轮。
3. PostgreSQL 在提交事务中检查源图归属、七牛转存状态和编辑深度。每条语音编辑链最多追加五代，达到上限后可以从一张新生成的图片开始。已经提交的语音轮使用稳定幂等键，重复交付时复用原任务。
4. 图片未完成、任务队列满、取消请求、参数不足或用户停止时结束当前轮。服务端的后台任务继续管理已提交的图片，App 可以查询任务详情。

## 语音事件与错误

已接受或查到的任务通过 WebSocket `agent.custom` 发送，事件 `data.type` 为 `image.task`。`data.image` 包含 `id`、`title`、`status`、`image_url`、`source_image_id` 和 `error_code`。`execution.progress` 及执行历史也包含可选的 `image` 字段。只有转存成功时才返回七牛地址。

可以在图像工作室管理标题和删除作品。上传参考素材时，工作室会先请求 `POST /files`，再用返回的素材 ID 提交生成或编辑任务。

服务日志记录请求处理阶段、图像编号、任务状态和异常类型。用户语音文本、生成提示词及供应商签名 URL 不写入图像操作日志。
