# Data Transfer（本地 rsync + SSH 传输工具）

按 `docs/PRD.md`（v3）实现的本机网页工具：Python 标准库后台 + 浏览器单页界面，
在本地与 `~/.ssh/config` 中的服务器之间用 `rsync + SSH` 传输文件/目录，支持多服务器、双向、
实时进度/速度/ETA/日志、停止与 `--partial` 续传。

## 依赖

- Python 3（仅标准库，无 Flask/Node/npm）
- `rsync`：**推荐 GNU rsync ≥ 3.1**（`brew install rsync`，已装在 `/opt/homebrew/bin/rsync`）。
  macOS 26 自带的 `/usr/bin/rsync` 是 openrsync，不支持 `--info=progress2`/`--no-owner` 等，
  工具会自动检测并降级为 `--progress` 兼容模式（进度为当前文件进度）。可用 `DT_RSYNC=/path/to/rsync` 指定。
- 系统 `ssh`、`/usr/bin/osascript`（文件夹选择器）、浏览器

## 运行

- **双击 `启动传输工具.command`**：后台用 `nohup` 独立运行（关闭终端窗口后仍可访问），
  通过 `/api/health` 版本号确认后自动打开浏览器；同目录同版本的后台会被复用，旧版本后台会先被停止。
- 停止后台：双击 `停止传输工具.command`（或 `kill $(cat .run/server.pid)`）。
- 命令行：`python3 transfer_tool.py [--port 8765] [--no-browser]`。
- 默认地址 `http://127.0.0.1:8765/`（只监听 127.0.0.1；端口被占用时依次尝试 8766–8772）。
- 直接打开 `web/index.html` 不是支持的用法。

## 使用说明

1. 本地地址：手动输入（支持 `~`、`$VAR`）或点文件夹图标调用系统“选取文件夹”，选择后立即写入
   `transfer_tool_config.json`，下次打开自动恢复。
2. 服务器地址：下拉多选，来自 `~/.ssh/config`（含 `Include`；不显示 `*`/`?`/`!` 规则），
   “刷新 config”重新读取；摘要用 ` / ` 拼接。**勾选顺序 = 传输顺序**。
3. 远程路径：只填服务器上的路径（禁止 `别名:`、`user@host:`、`ssh://`）。
4. 方向：本地 → 服务器 / 服务器 → 本地。多服务器下载自动放入 `本地目录/<别名>/`。
5. 开始传输后可“停止传输”（向 rsync 进程组发送 SIGINT），再次开始即续传：已完成文件被跳过，
   `--partial` 保留的未完成文件会重新传输（`--whole-file` 下按整文件续传）。

路径语义：上传本地**目录**时同步的是目录内容（源路径自动加 `/`），即“本地 mono-2 的内容 → 远程路径”；
下载时远程路径按原样交给 rsync（以 `/` 结尾表示下载目录内容，不带 `/` 表示把该目录本身放进本地目录）。
rsync 只会创建最后一级目标目录，远程父目录需已存在。

## 测试

```bash
python3 -m unittest discover -s tests -v      # 23 个单元/集成测试
python3 scripts/e2e_check.py                  # 端到端：真实后台进程 + 真实 rsync（GNU）
python3 scripts/e2e_check.py --rsync /usr/bin/rsync --port 8800   # openrsync 兼容模式
```

**测试不会连接任何真实服务器，也不读取/修改 `~/.ssh`**：`tests/fake_ssh.sh` 代替 ssh，
把每个“主机”映射为沙盒目录（`$FAKE_REMOTE_ROOT/<host>/`，`bad-*` 主机模拟连接失败），
rsync 本身完整运行。测试覆盖：SSH config/Include/循环/通配符/去重、路径校验与中文报错、
PRD 规定的 rsync 参数、地址拼接、多服务器上传（逐台、按顺序）、单/多服务器下载与按别名分目录、
部分失败/全部失败、进度/速度/ETA、运行中重复启动被拒、停止后保留部分文件并续传、配置持久化与读写错误、
文件夹选择器成功/取消/出错/缺失、静态文件路径穿越防护。

### 测试模式（演示界面用）

```bash
scripts/make_sample_data.sh 40 6         # 生成 sample_data/mono-2（6×40MB 模拟视频 + json）
scripts/run_test_mode.sh 25m             # 独立实例 http://127.0.0.1:8775/，限速 25MB/s，演示主机 demo-*
kill $(cat .run/test_server.pid)         # 停止测试实例
```

测试模式页面右上角显示“测试模式 · 模拟 SSH”，只使用 `tests/fixtures/ssh_config_demo` 中的演示主机。

## 文件结构

```
transfer_tool.py            HTTP 服务、SSH config 解析、选择器、配置、rsync 进程与状态
web/index.html|style.css|app.js
启动传输工具.command / 停止传输工具.command
transfer_tool_config.json   最近一次输入（运行后生成）
docs/PRD.md                 需求文档
tests/                      unittest、fake_ssh.sh、fixtures
scripts/                    样例数据、测试模式、e2e；scripts/dev/ 为截图用开发辅助工具
screenshots/                实机截图
```

## 与 PRD 的差异 / 假设

- macOS 自带 openrsync 不支持 PRD 的 `--info=progress2` 等参数：已安装 Homebrew GNU rsync 3.5.2 并优先使用，同时保留自动降级。
- 状态胶囊额外有“已停止”（PRD 7.4 要求的停止状态）。页面右上角在测试模式下多一个“测试模式”标识。
- `/api/health` 额外返回 `pid`、`app_dir`、`test_mode`，供启动脚本判断是否复用；另有 `POST /api/config`（保存部分字段）。
- 运行中本地地址、远程路径、方向、服务器选项会被锁定，避免与正在执行的任务不一致。
- 日志中 rsync 进度行按每 10% 记录一次，避免刷屏；上限 3000 行。
- 停止时先发 SIGINT，8 秒仍未退出再发 SIGTERM。
- 未对真实服务器做传输验证（安全要求）；真实 SSH 链路仅依赖 `ssh <别名>`，与 `~/.ssh/config` 一致。
