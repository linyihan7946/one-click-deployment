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
