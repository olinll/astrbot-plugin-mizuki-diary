# astrbot-plugin-mizuki-diary

通过 AstrBot / QQ 指令远程管理 [Mizuki-Content](https://github.com/olinll/Mizuki-Content) 的 `data/diary.ts`：增删改查日记、自动上传图片，本地暂存后一键推送到 GitHub。

## 特性

- **本地暂存 → 二次确认 → GitHub 单次 commit**（diary.ts + 新增图片同属一次提交）
- **多轮对话**新增 / 修改日记，支持图片、标签、心情、地点
- **删除 = 注释**（`/* { ... }, */`），可随时恢复；git 历史完整保留
- **图片自动处理**：下载 → 统一转 WebP → 按 `日期-日记ID-序号.webp` 命名
- 白名单权限控制
- 每次操作都重新拉取远程文件，避免覆盖冲突

## 安装与配置

1. 把本仓库放到 AstrBot 的 `data/plugins/` 下
2. 在 AstrBot WebUI 的插件配置里填写：
   - `github_token`：GitHub Personal Access Token（Contents: Read & Write）
   - `github_repo`：默认 `olinll/Mizuki-Content`
   - `github_branch`：默认 `master`
   - `allowed_user_ids`：允许使用的 QQ 号列表（**留空则无人可用**）
   - 其他项有合理默认，按需调整
3. 依赖：`pip install -r requirements.txt`（aiohttp / json5 / Pillow）

## 指令

| 指令 | 说明 |
|------|------|
| `/diary help` | 查看帮助 |
| `/diary list [page]` | 列出日记（含 `[已删]` 标记） |
| `/diary preview <id>` | 预览一条（含图片） |
| `/diary add` | 新增（多轮对话） |
| `/diary edit <id>` | 修改（字段菜单） |
| `/diary del <id>` | 删除（注释掉） |
| `/diary restore <id>` | 恢复已删除 |
| `/diary diff` | 查看本地 pending 改动 |
| `/diary discard` | 放弃所有 pending |
| `/diary push` | 推送到 GitHub（回复 `确认` 生效） |
| `/diary cancel` | 多轮对话中取消 |

## 多轮对话约定

- 内容步骤：可多条消息累积，`/done` 完成
- 可选字段：`skip` / `跳过` 跳过
- 图片步骤：直接发图片，`/done` 结束，`skip` 跳过，`clear` 清空（edit 时）
- 超时：`session_timeout` 秒无响应自动取消（默认 300）
- 中途退出：`/diary cancel`

## 数据约定

- diary 条目结构与 `data/diary.ts` 一致：`id / content / date / images? / location? / mood? / tags?`
- `id` 新增时自动 = 当前最大 + 1
- `date` 默认东八区当前时间（`Asia/Shanghai`），可手动覆盖
- 图片存仓库 `images/diary/` 目录，条目里用 `/images/diary/xxx.webp` 引用
- 不支持 GIF（发送时会报错提示）

## 实现要点

- 解析 `diary.ts` 用状态机定位 `const diaryData: DiaryItem[] = [ ... ];`，其中对象用 [json5](https://pypi.org/project/json5/) 解析，注释块 `/* { ... } */` 识别为已删除条目
- 只重生成 `diaryData` 这一块，文件其他部分（import、interface、helper 函数）原样保留
- GitHub 操作走 Git Data API（blob → tree → commit → ref），保证单次原子提交
- pending 改动持久化在 `data/plugin_data/astrbot_plugin_mizuki_diary/pending.json`，图片缓存在同目录 `image_cache/`

## 注意

- `github_token` 请用 fine-grained token 并限制到目标仓库
- 推送时会**重新拉取远程文件**应用 patches；如果 push 间隙你在 GitHub 网页手动改过 `diaryData` 块，会被覆盖
- 图片转 WebP 用 Pillow，少数带动画/奇特色彩空间的图可能丢失效果
