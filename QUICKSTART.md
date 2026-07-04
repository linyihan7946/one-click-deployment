# 快速开始指南

## 5 分钟完成首次部署

### 前置条件

1. **本地**: Windows PowerShell 5.1+ (Win10 自带)
2. **远程服务器**: Linux 且已安装 Docker + Docker Compose

> 服务器未安装 Docker？执行以下命令:
> ```bash
> curl -fsSL https://get.docker.com | sh
> sudo systemctl enable --now docker
> ```

### 第一步: 初始化配置

打开 PowerShell，进入项目目录:

```powershell
cd e:\GitHubWorkSpace\one-click-deployment
.\deploy.ps1 -Action init
```

按提示输入:

```
[1/5] 服务器 IP 或域名: 192.168.1.100
[2/5] SSH 端口 (默认 22): 22
[3/5] SSH 用户名: root
[4/5] 认证方式 (1=密码 2=密钥): 1
[5/5] 远程部署根路径: /opt/deploy
```

如果选择了密码认证，会提示输入服务器密码，用于推送 SSH 公钥。
完成后，后续部署无需再输入密码。

### 第二步: 修改 docker-compose.yml

编辑项目根目录的 `docker-compose.yml`（或创建你自己的），定义你的服务。

适合直接部署的项目:

- 单体 Web 服务: Python/Node.js/Java/Go/PHP/.NET 等。
- 前端静态站点: Vue/React/Vite/普通 HTML/CSS/JS。
- 已经能用 `docker compose up` 在本地跑起来的项目。

带数据库的项目建议项目自己维护 `docker-compose.yml`，不要完全依赖自动模板。例如:

```yaml
services:
  app:
    build: .
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

注意:

- 数据库服务不要暴露公网端口，后端通过 `db:5432` 这类服务名连接。
- 数据库必须使用 volume 持久化数据。
- 密码和连接串不要提交到公共仓库，生产环境建议通过 `.env` 或部署环境变量提供。
- 部署前先在本地执行 `docker compose up --build` 验证。

### 第三步: 一键部署

```powershell
.\deploy.ps1
```

就这么简单！脚本会自动:
1. 测试 SSH 连接
2. 将代码同步到服务器 `/opt/deploy/web/`
3. 远程执行 `docker compose build`
4. 远程执行 `docker compose up -d`
5. 显示容器状态

### 常用操作

| 操作 | 命令 |
|------|------|
| 部署 | `.\deploy.ps1` |
| 查看状态 | `.\deploy.ps1 -Action status` |
| 查看日志 | `.\deploy.ps1 -Action logs` |
| 修改配置 | `.\deploy.ps1 -Action config` |
| 回滚 | `.\deploy.ps1 -Action rollback` |
| 部署指定模块 | `.\deploy.ps1 -Module api` |
| 部署指定服务器 | `.\deploy.ps1 -Server staging` |
| 跳过 build | `.\deploy.ps1 -SkipBuild` |

## 多模块部署

假设项目结构:

```
my-project/
├── frontend/    ← web 模块
├── backend/     ← api 模块
└── admin/       ← admin 模块
```

配置 `deploy-config.json`:

```json
{
  "modules": {
    "web": {
      "sourcePath": "./frontend",
      "serverPath": "web"
    },
    "api": {
      "sourcePath": "./backend",
      "serverPath": "api"
    },
    "admin": {
      "sourcePath": "./admin",
      "serverPath": "admin"
    }
  }
}
```

分别部署:

```powershell
.\deploy.ps1 -Module web     →  /opt/deploy/web/
.\deploy.ps1 -Module api     →  /opt/deploy/api/
.\deploy.ps1 -Module admin   →  /opt/deploy/admin/
```

每个模块独立运行自己的 `docker-compose.yml`，互不干扰。
