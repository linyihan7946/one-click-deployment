# Agents - 一键部署工具

## 项目概述

通过 SSH + rsync/scp 将本地代码一键推送到远程服务器，并自动执行 `docker compose up -d` 完成部署。
提供 **PowerShell CLI** 和 **Web 可视化界面** 两种使用方式。

## 技术栈

| 层 | 技术 |
|---|---|
| Web 后端 | Python 3 + Flask 3.0 |
| Web 前端 | 原生 HTML/CSS/JS（暗色主题，响应式布局） |
| CLI | PowerShell 5.1+ |
| 部署引擎 | Docker Compose + SSH + rsync/scp |
| 通信协议 | REST API (JSON) |

## 项目结构

```
one-click-deployment/
├── server.py                # Flask Web 服务主入口（API + 部署引擎）
├── start.bat                # Windows 一键启动脚本
├── deploy_config.json       # 部署配置文件（服务器/模块/设置）
├── requirements.txt         # Python 依赖
├── README.md                # 完整使用文档
├── QUICKSTART.md            # 5 分钟快速开始
├── templates/
│   ├── docker-compose.yml   # Docker Compose 模板（web 模块）
│   ├── Dockerfile           # Node.js Dockerfile 模板
│   └── index.html           # Web 前端页面（含 JS 逻辑）
└── scripts/
    └── schema.json          # deploy-config.json 的 JSON Schema 校验
```

## API 端点

### 配置管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 获取完整配置（密码脱敏） |
| POST | `/api/config/servers` | 添加/更新服务器配置 |
| DELETE | `/api/config/servers/<name>` | 删除服务器配置 |
| POST | `/api/config/modules` | 添加/更新模块配置 |
| DELETE | `/api/config/modules/<name>` | 删除模块配置 |
| POST | `/api/config/settings` | 更新全局设置 |

### 项目检测

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/detect-project-type` | 自动检测项目类型 |
| POST | `/api/browse` | 浏览本地目录 |

### SSH 测试

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ssh-test` | 测试 SSH 连接 |

### 部署

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/deploy` | 执行部署（异步启动） |
| GET | `/api/deploy/status` | 获取部署进度和日志 |

### 日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/logs` | 获取日志文件列表 |
| GET | `/api/logs/<filename>` | 获取日志文件内容 |

## 配置结构

### deploy_config.json

```json
{
  "servers": [
    {
      "name": "服务器名称",
      "host": "IP 或域名",
      "port": 22,
      "user": "SSH 用户名",
      "password": "SSH 密码",
      "key_path": "SSH 私钥路径（可选）",
      "deploy_path": "/opt/deploy"
    }
  ],
  "modules": [
    {
      "name": "模块名称",
      "source_path": "本地项目路径",
      "server_path": "远程子路径",
      "port": 3000,
      "project_type": "nodejs|python|vue|java|go|php|dotnet|static"
    }
  ],
  "settings": {}
}
```

## 部署流程

```
前端触发部署
    ↓
API 验证参数 (server_name, module_name, source_path)
    ↓
Step 1: 测试 SSH 连接 ────────────────────── 10%
Step 2: 检查远程 Docker ──────────────────── 20%
Step 3: 检查 Docker Compose ──────────────── 30%
Step 4: 创建远程目录 ─────────────────────── 40%
Step 5: 同步文件 (rsync 优先, 失败则 scp+tar) ─ 50-70%
Step 6: 生成 docker-compose.yml + Dockerfile ─ 80%
Step 7: 远程 docker compose pull/build/up ─── 90%
Step 8: 获取容器状态 ─────────────────────── 100%
```

## Docker 模板库

Web 前端内置 8 种项目类型的 Docker 模板：

| 类型 | Dockerfile 基础镜像 | 默认端口 | Compose 服务名 |
|---|---|---|---|
| **Node.js** | node:20-alpine | 3000 | app-nodejs |
| **Python** | python:3.12-slim + gunicorn | 8000 | app-python |
| **Vue/React** | node:20-alpine → nginx:alpine (多阶段) | 80 | app-frontend |
| **Java** | eclipse-temurin:21-jre-alpine + maven | 8080 | app-java |
| **Go** | golang:1.22-alpine → alpine (多阶段) | 8080 | app-go |
| **PHP** | php:8.3-apache | 80 | app-php |
| **.NET** | dotnet/sdk:8.0 → dotnet/aspnet:8.0 (多阶段) | 8080 | app-dotnet |
| **静态网站** | nginx:alpine | 80 | app-static |

## 文件同步策略

1. **首选 rsync**: 增量传输，支持 exclude 规则
2. **回退 scp + tar**: 先本地压缩 → scp 上传 → 远程解压
3. **排除规则**: `.git`, `node_modules`, `.env`, `*.log`, `.claude`, `.vscode`, `__pycache__`, `.deploy-logs`, `.deploy-ssh`

## 项目类型检测规则

`/api/detect-project-type` 通过扫描目录下的特征文件自动识别：

| 特征文件 | 识别类型 |
|---|---|
| `requirements.txt`, `pyproject.toml`, `Pipfile`, `setup.py`, `manage.py`, `app.py`, `main.py` | Python |
| `pom.xml`, `build.gradle`, `settings.gradle` | Java |
| `go.mod` | Go |
| `*.csproj`, `*.sln` | .NET |
| `composer.json` | PHP |
| `package.json` + vue/react 依赖 | Vue/React |
| `package.json`（无框架依赖） | Node.js |
| `index.html` | 静态网站 |

## 安全注意事项

- 配置文件 `deploy_config.json` 中包含明文密码，**不应提交到公共仓库**
- Web API 的 `/api/config` 返回时会对密码字段做脱敏处理（替换为 `***`）
- SSH 连接使用 `-o StrictHostKeyChecking=no`，首次连接自动信任主机
- 远程命令通过 base64 编码传输，避免特殊字符注入
- `sshpass` 用于密码认证场景，生产环境建议使用密钥认证

## 开发约定

- Python 代码使用 4 空格缩进，函数注释采用 `"""三引号"""` docstring
- 前端 JS 使用原生 ES6，无框架依赖，所有逻辑在 `index.html` 内联
- CSS 使用 CSS 变量（`--primary`, `--bg` 等），暗色主题
- API 请求统一使用 `fetch` + `application/json`
- 所有路径拼接使用 `os.path.join`（Python）或模板字符串（JS）
- 部署状态通过全局 `deploy_status` 字典 + `threading.Lock` 实现线程安全
- 修改 Web 页面、后端接口或依赖后，如果本地 `http://localhost:5000` 已有旧服务在运行，必须先关闭旧的 Flask 进程，再用当前工作区代码重启服务，避免浏览器仍看到旧页面或旧接口。

## 快速启动

```powershell
# 方式 1: 使用启动脚本
.\start.bat

# 方式 2: 手动启动
pip install flask requests
python server.py

# 访问 Web 界面
http://localhost:5000
```
