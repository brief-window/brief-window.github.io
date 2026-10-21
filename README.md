# Brief Window

独立的信息窗口：将美卡论坛「玩卡、旅行、理财」及其公开子分类最近 **3 小时**的新帖子整理成静态 HTML。按话题分组，保留原始正文、用户名、发帖时间、Topic ID、Post ID、楼层和原帖链接；不生成摘要。

网站：<https://brief-window.github.io/>。仓库归 `brief-window` 组织所有，使用组织的默认 Pages 域名。

## 抓取方式

- 匿名访问 `/site.json` 获取分类和父分类关系。
- 从 `/posts.json` 开始，以每页最小 Post ID 作为下一页的 `before`。
- 正文优先使用完整 `raw` 字段，缺少时从完整 `cooked` 转成文字；不使用截断的 `excerpt`。
- 时间按 `created_at` 筛选，范围为生成时间之前 3 小时，排除未来、隐藏、删除和非普通帖子。
- 整页早于截止时间时停止；单个旧帖不会提前终止分页。此方法依赖论坛新建 Post ID 与发帖时间通常一致；不扫描任意历史导入记录。
- 请求间隔至少 1.5 秒；对 429 和服务端错误有限重试。`cloudscraper` 不保证现代 Cloudflare 挑战一定可通过；没有登录 cookie、代理或 CAPTCHA 服务。
- 达到分页上限、分类不匹配、游标失效或请求失败时不发布部分数据。发布空的错误状态页替换上一次内容，并将 workflow 标记为失败。

## 本地运行

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python collector.py
```

输出：`public/index.html`、`public/feed.json`、`public/status.json`、`public/reader.html`、`public/robots.txt`、`public/.nojekyll`。`reader.html` 是不足 1 KiB 的 HTML 状态入口，只包含生成时间、计数、状态和正文链接，可用来区分网页工具访问失败与正文大小问题。`feed.json` 保留本轮全部原始正文、Post ID、Topic ID、分类和时间。`public/` 被 Git 忽略，帖子正文不会进入 Git 历史。

## GitHub Pages

1. Settings → Pages → Source 选 **GitHub Actions**，Custom domain 留空。
2. Actions → **Refresh Brief Window** → **Run workflow**。
3. 查看 <https://brief-window.github.io/> 和 <https://brief-window.github.io/status.json>。

自动刷新由 cron-job.org 每小时的第 `3,13,23,33,43,53` 分钟通过 GitHub REST API 触发 `refresh.yml` 的 `workflow_dispatch`，间隔为 10 分钟。GitHub 原生 `schedule`（第 `4,14,24,34,44,54` 分钟）暂保留为备用，可能延迟或丢弃。

外部自动触发和手动运行在 GitHub 中都显示为 `workflow_dispatch`；是否按时自动触发，应查看 cron-job.org 的执行历史。请求返回成功只表示 GitHub 接收了触发请求，还需确认 Actions 完成，并检查页面 `Generated UTC` 和 `status.json` 的 `generated_at` 已更新。取消运行、部署或平台故障也可能让旧页面继续存在。

定时服务使用限定到本仓库、仅授予 Actions 读写权限的 fine-grained token。凭据保存在定时服务配置中，不写入仓库；到期前须在服务中替换。

## 通过 GitHub 连接读取

当 GPT 网页工具无法访问 Pages 时，可以通过 GitHub 连接下载 Actions 的 `brief-window-feed` artifact，其中只有一份 `feed.json`。这条路径需要读取 Actions 和处理下载文件的能力，不能用“能够读取仓库代码”代替实际验证。

1. 通过 GitHub 连接读取本仓库 `main` 分支根目录的 `latest.json`。这个固定入口提供本轮 `run_id`、`artifact_id`、ZIP 的 SHA-256、生成时间和计数，只包含元数据，不是帖子正文。优先用 `artifact_id` 直接下载 `brief-window-feed`，无需每轮自行寻找运行列表。目录在完整上传后发布；其所属工作流可能仍在完成发布和清理。
2. 下载 ZIP 并读取其中唯一的 `feed.json`，核对 ZIP 的 SHA-256 和目录一致。检查 `ok` 为 `true`、`error` 为 `null`，且 `generated_at` 距当前时间不超过 30 分钟；否则报告抓取失败或数据过旧。目录的生成时间和帖子/主题计数必须与正文文件一致。
3. 核对 `posts` 数量等于 `total_posts`，Post ID 唯一，Topic ID 去重数量等于 `total_topics`，发帖时间位于 `cutoff` 和 `generated_at` 之间。依据 `posts[*].text` 分析完整原文，不能只读取状态计数。

目录不存在、过期或下载返回 404 时，重新读取最新目录。备用路径是通过通用 GitHub `fetch` 读取 `https://api.github.com/repos/brief-window/brief-window.github.io/actions/runs?per_page=20`，选择最近成功完成的 `Refresh Brief Window` 运行，再列出和下载其 `brief-window-feed` artifact。`fetch_commit_workflow_runs` 只筛选 PR 运行，不适合作为这里的运行列表。不能因为没有专用的运行列表工具就直接宣称 GitHub 连接不支持读取。

每次 GPT 自动任务最多做 6 轮实际读取尝试，失败后等待 10、20、40、60、60 秒（有可用等待能力时），本轮恢复时间上限为 10 分钟。失败时报告实际错误、步骤和尝试次数；本轮结束不等于停止计划，后续每小时继续自动尝试，只有用户明确要求才允许暂停或删除计划。工具持续缺失时也不能用旧数据冒充成功。提示规则约束助手行为，并非平台层面剥夺其管理计划的能力，仍须用真实定时运行验收。

示例请求：“通过 GitHub 连接读取 brief-window/brief-window.github.io 的 main/latest.json，下载其中指定的 brief-window-feed artifact，解压并完整验证 feed.json 后分析最近三小时的帖子。失败最多尝试6轮，不自行暂停后续每小时任务。”

`reader_index.py` 由独立的 `publish-reader-index` job 执行：先验证上传文件的名称、所属运行和摘要，发布并读回 `latest.json` 后才删除更早的同名快照。网络、限流和服务端错误最多请求 5 次；目录写入冲突和读回延迟也有多次恢复机会。发布或读回失败时保留原有快照，供下一轮继续恢复。只有这个目录发布 job 新增 `contents: write`，用于更新本仓库的元数据文件；定时服务保存的 token 权限不变。

## 可见性与保留范围

源代码仓库与 Pages 网站均公开。页面带 `noindex, nofollow, noarchive`。域名根路径 `/robots.txt` 允许 `ChatGPT-User` 和 `OAI-SearchBot` 读取全部路径；其他爬虫只能读取不含帖子正文的 `/reader.html` 诊断页，其余路径仍被禁止。该单页例外用于排查网页工具是否因通用禁止规则而无法访问。robots 和页面元数据都是爬虫指引，不是访问控制，也不保证第三方不会收录或保存内容。

每次生成替换整个 3 小时快照，不保存本项目的长期 archive。Pages 临时 artifact 部署后删除。读取用的 `brief-window-feed` artifact 在新目录成功发布并读回后清理较早的同名副本，通常只保留最新一份；发布或清理异常时由一天 retention 兜底。Git 历史中的 `latest.json` 只包含编号、时间、摘要和计数，不包含帖子正文。GitHub 内部部署副本、CDN 缓存、第三方下载不受本项目控制。workflow 停止后，页面和读取 artifact 不会立刻自动删除，因此这不是严格的 3 小时 TTL，读取方必须核对生成时间。

浏览器或普通 HTTP 请求可以读取页面，不代表 GPT 的网页读取工具一定能直接访问；需单独验证兼容性。OpenAI 的搜索读取与训练爬虫设置相互独立；用户主动访问的 `ChatGPT-User` 请求也不一定适用 robots 规则。robots 更新可能需要时间被读取工具识别，不能把部署成功当作读取成功。参考：[OpenAI 爬虫说明](https://developers.openai.com/api/docs/bots)。

参考：[Discourse API](https://docs.discourse.org/#tag/Posts/operation/listPosts)、[Discourse 最新帖子查询](https://github.com/discourse/discourse/blob/main/lib/latest_posts_query.rb)、[GitHub Pages workflow](https://docs.github.com/en/get-started/start-your-journey/deploying-your-website-automatically)、[定时任务延迟](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times)。
