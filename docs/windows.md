# Windows 部署与排错

## 1. 选择运行路线

| 路线 | 需要什么 | 能演示什么 | 本次验证状态 |
|---|---|---|---|
| 原生 replay | Windows x64 + Python 3.12 | 完整调查、证据、审批、恢复、评测 | 代码在 Linux 实测；Windows 依赖轮子已解析下载；Windows 执行未测 |
| Docker replay | Docker Desktop + Linux containers | PostgreSQL + API + worker | 已交付配置，当前环境无 Docker，未执行 |
| Docker live | 上一行 + 内存建议 8 GB 以上可用 | 两个服务、指标／日志／链路采集、故障注入 | 已测试适配器协议与 demo handler；全栈未执行 |

建议先完成第一条，再学习其他两条。项目不是 Windows 服务安装器，当前用终端前台进程或 Docker Compose 运行。

## 2. 原生安装

在 [Python 官方网站](https://www.python.org/downloads/windows/) 安装 Python 3.12 的 64 位版本。打开新的 PowerShell：

```powershell
py -3.12 --version
cd C:\Projects\aegisops
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 setup
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 demo
```

`setup` 会创建虚拟环境、安装锁定依赖、安装本项目、生成本地随机 token，并运行数据库迁移。它不会覆盖已有的非空 token。所有脚本切换到工程根目录，路径有空格也使用引号处理。

随包 `wheelhouse/` 仅面向 Windows x64 / CPython 3.12。不要把整个 `.venv` 从另一台电脑复制过来，虚拟环境包含机器路径。代码可用于其他系统，其他系统按 requirements 在线安装。对 Windows ARM64、Python 3.13/3.14，不提供离线轮子兼容承诺。

## 3. 不使用 PowerShell 脚本

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -m aegisops init
.\.venv\Scripts\python.exe -m aegisops demo-replay
```

不需要执行 `Activate.ps1`。直接使用虚拟环境的 Python，避免激活策略和 PATH 混乱。

## 4. API 操作顺序

1. `python -m aegisops api` 启动 API。
2. 另开窗口 `python -m aegisops worker` 启动后台消费者；这里的 python 都替换成 `.\.venv\Scripts\python.exe`。
3. 打开 `http://127.0.0.1:8000/docs`。
4. 在编辑器中打开 `.env`，复制 viewer token 的值；如果文件用单引号包围，复制时不要带引号。
5. 点击 Authorize，完成 README 中的调用顺序。
6. `Ctrl+C` 停止进程。运行中的调查留在数据库内，重新启动 worker 后可以继续。

停止 worker 不等于取消任务。主动取消使用 `POST /runs/{run_id}/cancel`，等 worker 到达安全检查点。已经提交的修复不会被“取消”撤销。

## 5. Docker replay

先安装并启动 Docker Desktop，确保 `docker version` 能看到 Server 信息、容器模式为 Linux。

```powershell
.\.venv\Scripts\python.exe scripts\configure_docker.py
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
```

在 Docker replay 模式中，API 仍是 `http://127.0.0.1:8000/docs`，数据库由容器内 PostgreSQL 提供。原生 SQLite 数据不会自动搬进去。Docker 的数据库密码由配置脚本随机生成；不要删除密码后继续复用旧数据库卷。

## 6. Docker live

```powershell
.\.venv\Scripts\python.exe scripts\configure_docker.py --live
docker compose --profile live config --quiet
docker compose --profile live up -d --build --wait
.\.venv\Scripts\python.exe scripts\live_demo.py
```

配置脚本设置 Docker 专用 source 与 OTLP 地址，不改变原生 SQLite 路线。live_demo 对库存服务启用固定延迟，持续访问订单服务，等待遥测导出，再提交 incident。它不自动批准任何修复。

`live_demo.py` 还检查报告中 metrics、logs、traces 是否都有非空结果；某个采集组件没工作时会明确失败，不把空报告当成功。故障开关保持开启，方便观察。结束时可以关闭整个栈，或用 operator 审批关闭演示故障。

```powershell
docker compose --profile live logs worker
docker compose --profile live logs order inventory
docker compose --profile live logs otel-collector tempo loki prometheus
docker compose --profile live down
```

`down` 保留 PostgreSQL 数据卷；`down -v` 会删除该项目的数据库数据，仅在你确定要清空演示时使用。

## 7. 常见问题

| 现象 | 原因／处理 |
|---|---|
| 找不到 `py` | 重开终端；重新安装 Python Launcher，或用真实 Python 3.12 的完整路径 |
| `No Python at ...` | 旧 `.venv` 绑定了另一台机器路径；移走旧虚拟环境后重新 setup |
| `No matching distribution` | 确认 Python 3.12 x64；离线轮子不适用于其他平台 |
| `No module named aegisops` | 使用项目 `.venv` 的 python，并从根目录重新 setup |
| PowerShell 阻止脚本 | 使用 README 的单进程 `-ExecutionPolicy Bypass` 命令，或第 3 节纯 Python 命令 |
| 8000 端口占用 | 停止另一实例，或原生用 `aegisops api --port 8001`；浏览器地址同步修改 |
| 401 | Swagger token 错误／带了变量名或引号；确认 API 读取的 `.env` 来自工程根目录 |
| 403 approve | viewer 没有审批权限；切换 operator；检查过期、状态、取消请求 |
| 一直 RECEIVED | 没有启动 worker，或两个进程的数据库 URL／工作目录不同 |
| WAITING_APPROVAL | 这是正常暂停，先审批，再让 worker 继续 |
| database table missing | 运行 `aegisops migrate`；不要只创建空 `.db` 文件 |
| Docker 密码认证失败 | 旧卷仍使用最初密码；恢复原密码，或确认可删除演示数据后重建卷 |
| live traces 为空 | 检查 collector → Tempo；生成流量后等至少一个批处理周期 |
| 真实模型 invalid_llm_json | 供应商未返回符合 schema 的 JSON；检查该模型是否支持兼容的 JSON mode |
| 真实模型 missing_usage | 供应商没返回 usage；实现拒绝把未知 token 数量记为 0 |
| PARTIAL | 查看 report.limitations 和 events，通常是预算用尽、重试耗尽或策略拒绝 |

## 8. Windows 验证清单

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 test
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 crash
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 eval
```

当前交付没有 Windows 执行宿主，因此以上命令的 Windows 现场结果为 NOT_MEASURED。`.github/workflows/ci.yml` 已配置 Windows/Ubuntu 两个平台；上传到你自己的 GitHub 后可运行实际 Windows job。这个项目支持的部署方式与“本次已在 Windows 实机验证”是不同的陈述。

