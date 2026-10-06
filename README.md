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

输出：`public/index.html`、`public/status.json`、`public/robots.txt`、`public/.nojekyll`。`public/` 被 Git 忽略，帖子正文不会进入 Git 历史。

## GitHub Pages

1. Settings → Pages → Source 选 **GitHub Actions**，Custom domain 留空。
2. Actions → **Refresh Brief Window** → **Run workflow**。
3. 查看 <https://brief-window.github.io/> 和 <https://brief-window.github.io/status.json>。

定时任务为每小时 UTC 的第 `4,14,24,34,44,54` 分钟。GitHub 可能延迟或丢弃 scheduled run；以页面 `Generated UTC` 和 `status.json` 为准。取消运行或平台故障也可能让旧页面继续存在。

## 可见性与保留范围

源代码仓库与 Pages 网站均公开。页面带 `noindex`，域名根路径 `/robots.txt` 禁止搜索引擎抓取；二者都是搜索引擎指引，不是访问控制，也不保证第三方不会收录或保存内容。

每次生成替换整个 3 小时快照，不保存本项目的长期 archive。Pages 临时 artifact 部署后删除；异常未删时由一天 retention 清理。GitHub 内部部署副本、CDN 缓存、第三方下载不受本项目控制。静态 Pages 无法在 workflow 停止后自动删除服务器端内容，因此这不是严格的 3 小时 TTL。

浏览器或普通 HTTP 请求可以读取页面，不代表 GPT 的网页读取工具一定能直接访问；需单独验证兼容性。

参考：[Discourse API](https://docs.discourse.org/#tag/Posts/operation/listPosts)、[Discourse 最新帖子查询](https://github.com/discourse/discourse/blob/main/lib/latest_posts_query.rb)、[GitHub Pages workflow](https://docs.github.com/en/get-started/start-your-journey/deploying-your-website-automatically)、[定时任务延迟](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times)。
