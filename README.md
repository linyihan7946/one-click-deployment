# 一键部署工具 - Docker Compose 远程部署

通过 SSH + rsync/scp 将本地代码一键推送到远程服务器，并自动执行 `docker compose up -d` 部署。

## 特性

- **首次配置向导**: 只需填写服务器 IP、用户名、密码、部署路径
- **自动 SSH 密钥**: 首次使用后自动配置密钥认证，后续无需重复输入密码
- **多模块支持**: 按路径区分模块（web、api、admin 等），独立部署
- **多服务器支持**: 可配置多个目标服务器
- **增量同步**: 优先使用 rsync 增量传输，失败自动切换 scp + tar
- **完整生命周期**: 部署、状态查看、日志查看、回滚

## 快速开始

### 1. 环境要求

- **本地**: Windows 10+ (PowerShell 5.1+) 或 WSL/Git Bash
- **远程服务器**: Linux (已安装 Docker + Docker Compose)
- **推荐安装**: [Git for Windows](https://git-scm.com/download/win) (提供 rsync/ssh)

> **服务器安装 Docker 快速命令:**
> ```bash
> curl -fsSL https://get.docker.com | sh
> sudo systemctl enable --now docker
> sudo usermod -aG docker $USER
> ```

### 2. 初始化配置

```powershell
.\deploy.ps1 -Action init
```

按照向导提示输入:
1. 服务器 IP 或域名
2. SSH 端口 (默认 22)
3. SSH 用户名
4. 认证方式 (密码/密钥)
5. 远程部署根路径

### 3. 部署

```powershell
# 部署默认模块到默认服务器
.\deploy.ps1

# 部署指定模块
.\deploy.ps1 -Module api

# 部署到指定服务器
.\deploy.ps1 -Server staging

# 跳过 build，直接 up
.\deploy.ps1 -SkipBuild
```

### 4. 其他命令

```powershell
.\deploy.ps1 -Action status        # 查看远程容器状态
.\deploy.ps1 -Action logs          # 查看远程容器日志
.\deploy.ps1 -Action config        # 管理配置
.\deploy.ps1 -Action ssh-setup     # 重新配置 SSH 密钥
.\deploy.ps1 -Action rollback      # 回滚部署
```

## 适合部署的项目类型

本工具适合把**已经可以用 Docker 运行的 Web 项目**同步到远程服务器，并通过 `docker compose up -d` 启动。推荐用于以下项目:

- **单体 Web 服务**: Flask/FastAPI/Django、Express/Koa/NestJS、Spring Boot、Go Web、PHP、ASP.NET Core 等。
- **前端静态站点**: Vue、React、Vite、Next.js 静态导出、普通 HTML/CSS/JS。
- **前后端分离项目**: 前端、后端可作为不同模块分别部署，或由项目自带 `docker-compose.yml` 统一编排。
- **带外部依赖的后端服务**: 数据库、Redis、对象存储等已经在云服务或其他服务器中运行，通过环境变量连接。

需要谨慎处理的项目:

- **后端 + 数据库 + Redis 等多服务项目**: 可以部署，但建议使用项目自己维护的 `docker-compose.yml`，不要完全依赖自动模板猜测。
- **需要系统依赖的项目**: 例如 LibreOffice、ffmpeg、字体、Playwright/浏览器内核等，需要在项目 Dockerfile 中明确安装。
- **部署到子路径的项目**: 例如访问 `/my-app/`，后端需要支持 `SCRIPT_NAME`、`PUBLIC_BASE_PATH` 或类似 base path 配置。
- **生产数据库项目**: 不建议把数据库端口暴露到公网，数据库数据必须使用 Docker volume 或外部云数据库持久化。

如果项目没有提供 Docker 配置，工具可以按项目类型生成基础模板；如果项目已经有 `Dockerfile` / `docker-compose.yml`，推荐优先使用项目自带配置。

## 带数据库的项目怎么用

带数据库的项目最可靠的方式是: **项目根目录自己提供一份部署用 `docker-compose.yml`，然后在部署页面粘贴或使用这份配置**。工具负责同步代码、写入远程配置、执行 `docker compose build/up` 和接入公网入口。

示例: 后端服务 + PostgreSQL 数据库:

```yaml
services:
  app:
    build: .
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql://app:change-me@db:5432/appdb
    expose:
      - "8000"

  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      - POSTGRES_DB=appdb
      - POSTGRES_USER=app
      - POSTGRES_PASSWORD=change-me
    volumes:
      - db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U app -d appdb"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  db-data:
```

使用建议:

- **公网入口服务放在 compose 的主服务中**，通常命名为 `app`、`web` 或 `api`。
- **应用服务使用 `expose` 声明内部端口**，由工具的网关转发访问；不建议直接写 `ports` 暴露应用端口。
- **数据库服务不要暴露公网端口**，后端通过 Docker Compose 服务名访问，例如 `db:5432`、`mysql:3306`、`redis:6379`。
- **数据库密码不要提交到公共仓库**，生产环境建议通过 `.env`、部署页面环境变量或服务器密钥管理提供。
- **数据库数据必须挂载 volume**，例如 `db-data:/var/lib/postgresql/data`，否则容器重建可能丢数据。
- **首次部署前先本地验证**: 在项目目录执行 `docker compose up --build`，确认服务、迁移、静态资源和健康检查都正常。
- **迁移命令由项目自己处理**，例如在应用启动脚本里执行 `alembic upgrade head`、`prisma migrate deploy`、`python manage.py migrate` 等。

对于已经使用云数据库的项目，不需要在 compose 里声明 `db` 服务，只要给后端配置正确连接串即可:

```env
DATABASE_URL=postgresql://user:password@your-db-host:5432/dbname
REDIS_URL=redis://your-redis-host:6379/0
```

## 配置说明

### 配置文件: `deploy-config.json`

```json
{
  "servers": {
    "production": {
      "host": "192.168.1.100",
      "port": 22,
      "user": "root",
      "deployPath": "/opt/deploy",
      "authType": "key"
    },
    "staging": {
      "host": "192.168.1.101",
      "port": 22,
      "user": "deploy",
      "deployPath": "/opt/deploy",
      "authType": "key"
    }
  },
  "modules": {
    "web": {
      "sourcePath": ".",
      "serverPath": "web",
      "composeFile": "docker-compose.yml",
      "services": [],
      "buildArgs": {},
      "env": {
        "NODE_ENV": "production"
      }
    },
    "api": {
      "sourcePath": "./api",
      "serverPath": "api",
      "composeFile": "docker-compose.yml",
      "services": ["api"],
      "buildArgs": {},
      "env": {}
    }
  },
  "settings": {
    "sshKeyPath": "~/.ssh/id_ed25519",
    "compressBeforeSend": true,
    "rsyncExcludes": [
      ".git/",
      "node_modules/",
      ".env",
      "*.log",
      ".deploy-ssh/",
      ".deploy-logs/"
    ],
    "defaultModule": "web",
    "defaultServer": "production"
  }
}
```

### 模块路径说明

模块部署到服务器的路径规则为: `<deployPath>/<serverPath>`

示例:
- 配置 `deployPath: /opt/deploy`，模块 `web` 的 `serverPath: web`
- 实际部署到: `/opt/deploy/web/`

每个模块目录下会包含:
```
/opt/deploy/web/
├── docker-compose.yml
├── Dockerfile
├── .env          (自动生成)
└── <源代码>
```

## 项目目录结构

```
one-click-deployment/
├── deploy.ps1              # 主部署脚本
├── deploy-config.json      # 部署配置
├── README.md               # 说明文档
├── templates/              # 模板文件
│   ├── docker-compose.yml  # Docker Compose 模板
│   └── .dockerignore       # 排除文件模板
├── .deploy-ssh/            # SSH 密钥 (自动生成)
└── .deploy-logs/           # 部署日志 (自动生成)
```

## 工作原理

```
┌──────────────┐     SSH + rsync/scp      ┌──────────────────┐
│  本地代码     │ ──────────────────────→  │   远程服务器      │
│              │    1. 压缩代码            │                  │
│  deploy.ps1  │    2. 传输到服务器        │   Docker         │
│              │    3. 远程解压            │   ┌──────────┐   │
│  配置信息     │    4. 远程 docker build   │   │ Container │   │
│  (IP/用户/路径)│   5. 远程 docker compose  │   └──────────┘   │
└──────────────┘    up -d                  └──────────────────┘
```

## 多模块部署示例

假设你有三个模块需要部署到不同路径:

```json
{
  "modules": {
    "web": {
      "sourcePath": "./frontend",
      "serverPath": "web",
      "composeFile": "docker-compose.yml"
    },
    "api": {
      "sourcePath": "./backend",
      "serverPath": "api",
      "composeFile": "docker-compose.yml"
    },
    "admin": {
      "sourcePath": "./admin-panel",
      "serverPath": "admin",
      "composeFile": "docker-compose.yml"
    }
  }
}
```

部署命令:
```powershell
.\deploy.ps1 -Module web     # 部署到 /opt/deploy/web/
.\deploy.ps1 -Module api     # 部署到 /opt/deploy/api/
.\deploy.ps1 -Module admin   # 部署到 /opt/deploy/admin/
```

## 故障排查

### SSH 连接失败
```powershell
# 测试 SSH 连接
ssh -i ~/.ssh/id_ed25519 user@host

# 重新配置 SSH 密钥
.\deploy.ps1 -Action ssh-setup
```

### rsync 不可用
脚本会自动切换到 scp + tar 模式，无需额外操作。

### 远程 Docker 未安装
在服务器上执行:
```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
```

### 权限问题
确保远程用户有 Docker 权限:
```bash
sudo usermod -aG docker $USER
# 重新登录使配置生效
```

## 高级用法

### 跳过 build 直接部署
适用于只修改了配置文件的场景:
```powershell
.\deploy.ps1 -SkipBuild
```

### 自定义 SSH 端口
在配置文件中修改 `port` 字段即可。

### 多环境部署
```json
{
  "servers": {
    "dev": { "host": "10.0.0.1", ... },
    "staging": { "host": "10.0.0.2", ... },
    "prod": { "host": "10.0.0.3", ... }
  }
}
```

```powershell
.\deploy.ps1 -Server dev
.\deploy.ps1 -Server staging
.\deploy.ps1 -Server prod
```
