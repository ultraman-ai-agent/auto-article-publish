# auto-article-publish

自动生成文章并发布到小红书，以及抓取财经新闻繁转简后发布到微信公众号草稿箱。

## 两种用法

### 可视化界面（推荐）

1. 双击 `run_gui.bat`（首次自动安装 `customtkinter` 界面依赖）。
2. 右上角可切换 深色 / 浅色 / 跟随系统。
3. 操作进行中有动态反馈：顶部进度条、按钮动态文字、发布流程步骤条（生成文案→配图→留档→发布）、状态点脉冲。
4. 首次到「配置」页签填写密钥并保存；同页可配置生成规则（配图数量范围、去重天数与阈值、图片来源）并编辑「提示词」。
5. 到「发布」页签点「启动服务」，再点「扫码登录」用小红书 App 扫描界面内二维码。服务栏的登录指示显示「已登录」即登录成功。
6. 输入主题 → 「生成并配图」→ 预览编辑 → 「发布」。

生成规则：内置“30 岁职场人”真实口吻提示词；与最近 7 天内容相似度 ≤30%，超阈值自动重新生成；标题/标签夸张博眼球；配图按范围随机、首图为封面。

配图来源：汽车主题会**自动按车型从汽车之家抓取**外观/内饰图（映射表 `data/car_series.json`，可用 `python scripts/build_car_map.py` 更新），其余内容用 Pexels/Unsplash；车站抓取失败自动回退图库。汽车之家图片存在版权/风控风险，请自行评估。

### 全自动发布

「自动」页签可配置固定时段、自动发布范围、选题来源与选题池；每日发布上限在「配置」页。点「启动常驻」即可无人值守定时发布（需保持程序运行）。

- 选题来源：`pool_first` 选题池优先（池空 AI 生成，推荐）/ `ai` 纯 AI / `pool_only` 仅选题池。
- 上限统计：当天“发布成功”条数（自动+手动都算）；已过时段不补发；可用「强制执行一次」忽略上限。
- 命令行：`python auto.py --once` 跑一篇，`python auto.py --once --force` 忽略上限，`python auto.py --loop` 常驻。
- 建议先用「仅自己可见」验证几篇再切换公开。

> 若未签名 exe 被 SmartScreen 拦截，`run_gui.bat` 会自动解除锁定；仍被拦时右键 exe → 属性 → 勾「解除锁定」。
>
> 若被 Defender 拦截（报 `leakless.exe` 相关错误），以管理员运行：
> `powershell -ExecutionPolicy Bypass -File .\scripts\allow_defender.ps1`
> 脚本会排除 Temp 下的 `leakless-amd64-*` 目录（关键），随后到 Defender「保护历史记录」把 `leakless.exe` 条目「允许/还原」。微软电脑管家也会拦同一文件，必要时在其设置里加信任。
>
> 发布长时间“处理中”会在 240 秒超时并提示确认；排查可在「配置」页把「静默启动浏览器」设为 `false` 后再启动服务，即可看到浏览器窗口。
>
> 上游 v2.5.0 的「回点标题输入框失败」发布卡死 Bug 已通过**编译社区补丁 PR #846** 修复（原版 exe 已备份）；「实时热点参考」因上游搜索接口不稳定默认关闭。

### 命令行

```bash
pip install -r requirements.txt
copy .env.example .env          # 填入模型与图库密钥
python publish.py "春天露营装备推荐" --dry-run
python publish.py "春天露营装备推荐"
```

## 微信公众号（新闻采集）

抓取 Yahoo 香港財經新闻 → MiniMax 繁转简 → 用 [Wechatsync](https://github.com/wechatsync/Wechatsync) 推送到公众号**草稿箱**（人工复核后再发布）。整条链路独立于小红书，互不影响。

前置：

1. 安装 Chrome/Edge 扩展 [Wechatsync](https://www.wechatsync.com/#install)，在扩展设置里启用「同步桥接/MCP 连接」并复制 Token。
2. 浏览器里登录公众号后台，确认扩展能识别到公众号账号。
3. `npm install -g @wechatsync/cli`，把 Token 填入 `.env` 的 `WECHATSYNC_TOKEN`。
4. 配置 `MINIMAX_API_KEY`（繁转简用，默认国内站 `api.minimaxi.com`）。

用法：

- 界面：顶部把平台切到「公众号」→ 填配置并保存 → 「检测环境」「检查登录」→「预览候选」→「干跑」确认无误后「抓取并发布」。
- 命令行：
  ```bash
  python wechat_publish.py --check          # 检查 wechatsync 是否可用
  python wechat_publish.py --auth           # 检查公众号登录状态
  python wechat_publish.py --list           # 只看候选新闻
  python wechat_publish.py --dry-run        # 抓取+翻译+留档，不推送
  python wechat_publish.py                  # 抓取并推送草稿
  python wechat_publish.py --from-md a.md   # 直接发布本地 Markdown
  ```

规则：每次随机抽 3~5 篇（数量可在 `.env` 调整）；命中 `NEWS_AI_KEYWORDS` 的 AI 相关新闻优先，且在候选中随机而非取前几条；正文图片一并抓取压缩，广告与来源杂质文本自动剔除；繁转简保持图文顺序。

新增站点：在 `sites/` 下新建模块，暴露名为 `SITE` 的 `NewsSite` 实例即可自动注册；在公众号面板的「新闻站点」下拉中选择。

> 合规提示：转载外部财经新闻到自有公众号存在版权与平台风控风险，请自行评估；系统默认推草稿，人工复核后再发布。

## 文档

- `docs/业务文档.md`：功能、场景、操作流程、待办。
- `docs/技术文档.md`：架构、配置、接口、排障。

## 规则

新增或修改功能必须同步维护上述两份文档，详见 `AGENTS.md`。
