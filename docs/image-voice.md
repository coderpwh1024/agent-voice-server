# 语音生成、编辑与查询图像

登录 App 后，在对话模式可以说「生成一张纸艺图像，画面里有一座绿色小山」。语音会播报图像编号，图像任务同时出现在图像工作室。可以说「查一下 12 号图片的状态」。等待图片完成后，可以接着说「把这张图的背景换成蓝色」，也可以在修改指令里报出图片编号。编辑会生成有独立编号的新版本。

可以说「查一下十二号图片的详情」「帮我找标题叫远山的图像」或「看看有没有画着蓝色小船的图片」。按画面关键词时检索作品的生成描述。单条结果会播报标题、编号、状态、生成描述摘要与已指定的尺寸。多个结果按作品编号从新到旧展示，语音说明匹配总数，每轮最多展示六张。打开卡片可查看详情，也可以在 10 分钟内紧接着说列表中的编号，例如「十八号」或「查一下 18 号」。查询范围为当前用户的未删除作品。

按编号、标题或画面关键词找不到图片时，语音会说明未命中，并显示当前用户最近三天创建的图片任务。也可以直接说「看看我最近三天的图片」。三天按查询时刻往前 72 小时计算，结果按创建时间倒序，最多展示六张。近期也没有记录时会明确提示。生成中和失败的记录显示真实状态，成图保存后会在语音首页的卡片中更新。

## 在 Nacos 管理 Skill

`manage-user-images` 位于 Nacos 控制台的「AI 注册中心 → Skill 管理」。仓库中的 [SKILL.md](../skills/manage-user-images/SKILL.md) 是发布源文件，包含生成、编辑、查询、追问和分步编辑的工作流。服务启动时下载配置指定的已发布版本，只有识别到语音图像请求时才将说明作为参考交给对话模型。鉴权、操作参数、幂等、并发与编辑上限由服务端执行。

发布或更新时，先上传，再提交审核。查看版本状态后，若版本已经 `online`，即可使用。若已通过审核且等待发布，再执行 `skill-release`。将下例的 `1.0.0` 改为本次实际上传版本。 [Nacos CLI 文档](https://nacos.io/en/docs/v3.2/manual/admin/nacos-cli/)

```sh
nacos-cli skill-upload ./skills/manage-user-images
nacos-cli skill-review manage-user-images --version 1.0.0
nacos-cli skill-describe manage-user-images
nacos-cli skill-release manage-user-images --version 1.0.0
```

将以下片段合并到 Nacos 主配置 `agent-voice.yaml`。确认 `image.enabled` 已启用，发布配置后重启后端。`version` 优先于 `label`，省略 `version` 时默认使用 `latest`。

```yaml
skills:
  image:
    enabled: true
    version: '1.0.0'
    timeout_seconds: 20
```

服务启动日志记录加载的 Skill 名称、版本或标签和内容摘要。启用时需要有效的已发布 Skill。关闭 `skills.image.enabled` 可暂停语音图像操作，App 图像工作室仍由 `image.enabled` 控制。

## 逐轮编辑 LOOP

1. 入口是经过认证和线程校验的语音对话。当前话语给出明确指令。用户可指定本人作品编号，或用「这张图」等说法指代当前图片。单张图片的指代在同一线程中有效 10 分钟，切换到其他话题或返回图片列表后，编辑时需重新报编号或先打开一张图片的详情。
2. 每轮最多解析一次模型意图并提交一个异步图片任务。多项可以同时完成的改动可合成一次编辑。需要依次完成的改动，由用户等当前版本保存后发起下一轮。
3. PostgreSQL 在同一事务中检查归属、七牛转存状态、当前版本和编辑额度。同一个根图最多提交五次编辑，从旧版继续仍占用同一个 LOOP 的额度。编辑版本写入独立表。已经提交的语音轮使用稳定幂等键，重复交付时复用原任务。
4. 用户可以说「就用十三号图片」定稿，或在当前图像上下文说「先不改了」结束 LOOP。达上限后仍能选择已保存版本定稿。服务端继续管理已提交的图片，App 可以查询任务和版本详情。

配置、表结构、部署和边界示例见 [语音图像编辑 LOOP](image-loop.md)。

## 语音事件与错误

单条任务通过 WebSocket `agent.custom` 发送，事件 `data.type` 为 `image.task`。`data.image` 包含 `id`、`title`、`status`、`image_url`、`source_image_id`、`error_code` 和可选的 `size`、`create_time`。语音编辑版本还携带 `loop_root_image_id` 和 `loop_version`。结束 LOOP 时发送 `image.loop`。多条结果和近三天列表使用 `image.list`，携带 `items` 和 `total`。`execution.progress` 及执行历史分别使用可选的 `image` 和 `image_results` 字段。只有转存成功时才返回七牛地址。

可以在图像工作室管理标题和删除作品。上传参考素材时，工作室会先请求 `POST /files`，再用返回的素材 ID 提交生成或编辑任务。

服务日志记录请求处理阶段、图像编号、任务状态和异常类型。用户语音文本、生成提示词及供应商签名 URL 不写入图像操作日志。
