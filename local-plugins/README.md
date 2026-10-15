# 本地插件仓

本目录是随仓库分发的 MoviePilot V3 本地插件源，布局与 `MoviePilot-Plugins` 一致：
`package.v3.json` 是索引，`plugins.v3/<插件ID小写>/` 是源码。

## 启用

1. 在系统设置（或 `app.env`）中把 `PLUGIN_LOCAL_REPO_PATHS` 设为 `local-plugins`
   （相对路径按 MoviePilot 根目录解析，也可填绝对路径；多个目录用英文逗号分隔）。
2. 打开「插件市场」，搜索插件名安装；安装会把源码复制到运行目录 `app/plugins/<插件ID小写>/`。
3. 修改源码后在插件市场重新安装（或开启 `PLUGIN_AUTO_RELOAD` 自动同步）。

本地开发时也可以直接把 `plugins.v3/<插件>` 复制到 `app/plugins/<插件>`，主程序启动即加载；
该目录被 `.gitignore` 忽略，不随仓库提交，源码仍以本目录为准：

```bash
rsync -a --delete --exclude __pycache__ local-plugins/plugins.v3/jackett/ app/plugins/jackett/
```

## 插件

### Jackett

把 Jackett 已配置的索引器作为“插件资源源”接入：

- 手动搜索、订阅搜索：以关键字（或 IMDb ID）请求 Torznab 接口，结果与 PT 站点结果一起展示和过滤。
- 订阅刷新：每轮拉取一次 Jackett 最新种子，进入独立缓存桶 `plugin` 参与订阅匹配。
  全局「订阅站点」设置只约束 PT 站点候选，不影响插件候选；订阅自身显式限定站点时，插件候选不参与该订阅的匹配。
- 下载：使用 Jackett 的 `/dl/` 代理链接或磁力链接，不需要站点 Cookie。

配置项：Jackett 地址、API Key、索引器多选（留空为全部，走 `all` 聚合端点）、请求超时、是否走系统代理、资源优先级。
