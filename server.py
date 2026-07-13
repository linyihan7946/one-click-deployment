"""
一键部署 - Web 可视化部署工具
Flask 后端服务
"""
import json
import os
import re
import shlex
import stat
import sys
import subprocess
import threading
import tempfile
import base64
import time
import io
import fnmatch
import ipaddress
from datetime import datetime
from flask import Flask, Response, render_template, request, jsonify

import paramiko

app = Flask(__name__)


def slugify_project_name(value):
    """Return a URL-safe project slug for path-based deployment."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    return slug or "app"


def normalize_public_domain(value):
    """Normalize a user-entered public domain for gateway routing."""
    domain = (value or "").strip()
    domain = re.sub(r"^https?://", "", domain, flags=re.IGNORECASE)
    domain = domain.split("/", 1)[0].split(":", 1)[0].strip().lower()
    return domain or "www.aigenimage.cn"


def is_ip_address(value):
    """Return True when the public host is an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(normalize_public_domain(value))
        return True
    except ValueError:
        return False


def gateway_domain_values(domain):
    """Return server_name and certificate basename for a public domain."""
    domain = normalize_public_domain(domain)
    if is_ip_address(domain):
        return domain, domain, domain
    if domain.startswith("www."):
        apex = domain[4:]
        server_names = f"{domain} {apex}"
        cert_base = apex
    else:
        server_names = f"{domain} www.{domain}"
        cert_base = domain
    return domain, server_names, cert_base


def extract_first_container_port(compose_content):
    """Best-effort extraction of the first container-side port from compose YAML text."""
    for line in compose_content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        value = stripped[1:].strip().strip("'\"")
        if not value:
            continue
        if ":" in value:
            value = value.split(":")[-1]
        value = value.split("/")[0]
        if value.isdigit():
            return value
    return ""


def count_compose_services(compose_content):
    """Best-effort count of services in a docker-compose YAML string."""
    in_services = False
    services_indent = 0
    count = 0

    for line in compose_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        if indent == 0:
            in_services = stripped == "services:"
            services_indent = indent
            continue

        if in_services and indent == services_indent + 2 and stripped.endswith(":"):
            count += 1

    return count


def normalize_compose_for_path_gateway(compose_content, project_slug=None):
    """Convert host port mappings to internal expose entries for path-based gateway routing."""
    lines = compose_content.splitlines()
    normalized = []
    in_ports = False
    ports_indent = 0
    changed = False
    env_inserted = "PUBLIC_BASE_PATH" in compose_content
    container_name_handled = False
    service_count = count_compose_services(compose_content)
    is_multi_service = service_count > 1
    in_services = False
    services_indent = 0
    current_service = ""
    app_like_services = {"app", "web", "frontend", "backend", "api", "server"}

    for idx, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        if indent == 0 and stripped:
            in_services = stripped == "services:"
            services_indent = indent
            current_service = ""
        elif in_services and indent == services_indent + 2 and stripped.endswith(":"):
            current_service = stripped[:-1].strip().strip("'\"")

        if in_ports and stripped and indent <= ports_indent and not stripped.startswith("-"):
            in_ports = False

        if stripped == "ports:":
            normalized.append(line.replace("ports:", "expose:", 1))
            in_ports = True
            ports_indent = indent
            changed = True
            continue

        if project_slug and not is_multi_service and stripped.startswith("container_name:") and not container_name_handled:
            normalized.append(f"{' ' * indent}container_name: {project_slug}-app")
            container_name_handled = True
            changed = True
            continue

        should_inject_env = not is_multi_service or current_service in app_like_services
        if project_slug and stripped == "environment:" and not env_inserted and should_inject_env:
            normalized.append(line)
            env_is_list = True
            for next_line in lines[idx + 1:]:
                next_stripped = next_line.strip()
                if not next_stripped or next_stripped.startswith("#"):
                    continue
                next_indent = len(next_line) - len(next_line.lstrip(" "))
                if next_indent <= indent:
                    break
                env_is_list = next_stripped.startswith("-")
                break
            if env_is_list:
                normalized.append(f"{' ' * (indent + 2)}- PUBLIC_BASE_PATH=/{project_slug}")
            else:
                normalized.append(f"{' ' * (indent + 2)}PUBLIC_BASE_PATH: /{project_slug}")
            env_inserted = True
            changed = True
            continue

        if in_ports and stripped.startswith("-"):
            value = stripped[1:].strip().strip("'\"")
            if ":" in value:
                value = value.split(":")[-1]
                changed = True
            value = value.split("/")[0]
            normalized.append(f"{' ' * indent}- \"{value}\"")
            continue

        normalized.append(line)

    return "\n".join(normalized) + ("\n" if compose_content.endswith("\n") else ""), changed


def normalize_dockerfile_for_python_gateway(dockerfile_content):
    """Ensure common Python/gunicorn templates contain the runtime they start."""
    if 'CMD ["gunicorn"' not in dockerfile_content and "CMD ['gunicorn'" not in dockerfile_content:
        return dockerfile_content, False

    changed = False
    lines = []
    pip_env_inserted = "PIP_INDEX_URL" in dockerfile_content
    for line in dockerfile_content.splitlines():
        lines.append(line)
        if not pip_env_inserted and line.strip().startswith("WORKDIR "):
            lines.append("")
            lines.append("ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \\")
            lines.append("    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn")
            pip_env_inserted = True
            changed = True
            continue

        stripped = line.strip()
        if stripped.startswith("RUN pip install") and "-r requirements.txt" in stripped and "gunicorn" not in stripped:
            lines[-1] = line.rstrip() + " gunicorn"
            changed = True

    return "\n".join(lines) + ("\n" if dockerfile_content.endswith("\n") else ""), changed

# 配置
def normalize_dockerfile_for_node_package(dockerfile_content, source_path):
    """Adjust generic Node Dockerfiles to the scripts declared by package.json."""
    lowered = dockerfile_content.lower()
    if "npm " not in lowered and "package" not in lowered and "node:" not in lowered:
        return dockerfile_content, []

    package_path = os.path.join(source_path, "package.json")
    if not os.path.isfile(package_path):
        return dockerfile_content, []

    try:
        with open(package_path, "r", encoding="utf-8") as f:
            package_data = json.load(f)
    except Exception:
        return dockerfile_content, []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else {}
    scripts = scripts if isinstance(scripts, dict) else {}
    has_build = bool(str(scripts.get("build", "")).strip())
    has_start = bool(str(scripts.get("start", "")).strip())
    main_entry = package_data.get("main") if isinstance(package_data, dict) else ""
    main_entry = main_entry if isinstance(main_entry, str) and main_entry.strip() else "server.js"

    # Check if a lock file exists; if not, fall back from npm ci to npm install
    has_lock_file = any(
        os.path.isfile(os.path.join(source_path, name))
        for name in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
    )

    lines = []
    messages = []
    removed_build = False
    rewrote_cmd = False
    fixed_npm_ci = False

    for line in dockerfile_content.splitlines():
        stripped = line.strip()
        if not has_build and re.match(r"^RUN\s+(npm\s+run|yarn|pnpm)\s+build\b", stripped):
            removed_build = True
            continue

        # If no lock file exists, replace npm ci with npm install (npm ci requires lock file)
        if not has_lock_file and re.match(r"^RUN\s+npm\s+ci\b", stripped):
            line = line.replace("npm ci", "npm install", 1)
            fixed_npm_ci = True

        if stripped.startswith("CMD "):
            command_looks_like_node_entry = (
                "dist/index.js" in stripped
                or "server.js" in stripped
                or re.search(r"['\"]node['\"]", stripped) is not None
            )
            if has_start and command_looks_like_node_entry and stripped != 'CMD ["npm", "start"]':
                indent = line[: len(line) - len(line.lstrip(" "))]
                lines.append(f'{indent}CMD ["npm", "start"]')
                rewrote_cmd = True
                continue
            if not has_start and "dist/index.js" in stripped and main_entry:
                indent = line[: len(line) - len(line.lstrip(" "))]
                lines.append(f'{indent}CMD ["node", "{main_entry}"]')
                rewrote_cmd = True
                continue

        lines.append(line)

    if removed_build:
        messages.append("已根据 package.json 移除缺失的 npm run build 构建步骤")
    if fixed_npm_ci:
        messages.append("未找到 lock 文件，已将 npm ci 替换为 npm install")
    if rewrote_cmd and has_start:
        messages.append("已根据 package.json 改用 npm start 启动 Node 服务")
    elif rewrote_cmd:
        messages.append(f"已根据 package.json main 字段改用 node {main_entry} 启动 Node 服务")

    return "\n".join(lines) + ("\n" if dockerfile_content.endswith("\n") else ""), messages


def detect_monorepo_services(path):
    """Detect subdirectories with Dockerfile/docker-compose.yml (monorepo pattern).

    Returns a list of dicts describing each service sub-project found.
    """
    services = []
    skip_dirs = {
        ".", "..", "node_modules", "__pycache__", ".git", ".svn", ".hg",
        ".vscode", ".idea", "logs", "dist", "build", "target", ".cache",
        ".pytest_cache", ".mypy_cache", ".ruff_cache",
    }
    try:
        entries = sorted(os.listdir(path))
    except Exception:
        return services

    for item in entries:
        sub_path = os.path.join(path, item)
        if not os.path.isdir(sub_path):
            continue
        if item.startswith(".") or item.lower() in skip_dirs:
            continue
        try:
            sub_files = {f.lower() for f in os.listdir(sub_path)}
        except Exception:
            continue

        has_dockerfile = "dockerfile" in sub_files
        has_compose = "docker-compose.yml" in sub_files or "docker-compose.yaml" in sub_files
        if not has_dockerfile and not has_compose:
            continue

        services.append({
            "name": item,
            "dir": item,
            "has_dockerfile": has_dockerfile,
            "has_compose": has_compose,
        })

    return services


def rewrite_compose_for_monorepo(compose_content, subdir):
    """Rewrite build contexts and relative paths in a sub-compose to be relative to monorepo root."""
    lines = compose_content.splitlines()
    result = []
    in_build_block = False
    build_indent = 0

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        # Track when we enter/leave a build: block
        if stripped == "build:":
            in_build_block = True
            build_indent = indent
            result.append(line)
            continue

        if in_build_block:
            if stripped and indent <= build_indent and not stripped.startswith("#"):
                in_build_block = False
            elif stripped.startswith("context:"):
                old_val = stripped.split(":", 1)[1].strip().strip("'\"")
                if old_val == ".":
                    new_val = f"./{subdir}"
                elif old_val.startswith("./"):
                    new_val = f"./{subdir}/{old_val[2:]}"
                elif old_val.startswith("../"):
                    new_val = old_val  # leave parent refs alone
                else:
                    new_val = f"./{subdir}/{old_val}"
                result.append(f"{' ' * indent}context: {new_val}")
                continue
            elif stripped.startswith("dockerfile:"):
                # dockerfile is relative to context — leave it unchanged
                result.append(line)
                continue

        # Rewrite env_file: relative paths
        if stripped.startswith("env_file:"):
            val = stripped.split(":", 1)[1].strip().strip("'\"")
            if val.startswith("./") or (val and not val.startswith("/") and not val.startswith("$")):
                # inline form — rewrite
                prefix = line[: len(line) - len(line.lstrip(" "))]
                new_path = f"./{subdir}/{val.lstrip('./')}" if not val.startswith("./") else f"./{subdir}/{val[2:]}"
                result.append(f"{prefix}env_file: {new_path}")
                continue

        # Rewrite env_file list items  - ./.env  or  - .env
        if stripped.startswith("- .") and ("env" in stripped.lower()):
            prefix = line[: len(line) - len(line.lstrip(" "))]
            val = stripped[1:].strip().strip("'\"")
            new_path = f"./{subdir}/{val.lstrip('./')}" if val.startswith("./") else f"./{subdir}/{val}"
            result.append(f'{prefix}- "{new_path}"')
            continue

        result.append(line)

    return "\n".join(result) + ("\n" if compose_content.endswith("\n") else "")


def merge_monorepo_compose(sub_services, source_path):
    """Merge sub-project docker-compose files into one root-level compose.

    For sub-projects with a docker-compose.yml, read and rewrite it.
    For sub-projects with only a Dockerfile, generate a minimal service entry.
    Returns (merged_compose_string, list_of_messages).
    """
    all_services_lines = []
    all_volumes_lines = []
    all_networks_lines = []
    messages = []
    seen_volumes = set()
    seen_networks = set()

    for svc in sub_services:
        subdir = svc["dir"]
        compose_path = None
        for name in ("docker-compose.yml", "docker-compose.yaml"):
            candidate = os.path.join(source_path, subdir, name)
            if os.path.isfile(candidate):
                compose_path = candidate
                break

        if compose_path:
            try:
                with open(compose_path, "r", encoding="utf-8") as f:
                    raw = f.read()
            except Exception as exc:
                messages.append(f"读取 {subdir}/{name} 失败: {exc}")
                continue

            rewritten = rewrite_compose_for_monorepo(raw, subdir)
            # Parse sections from the rewritten compose
            current_section = None
            for line in rewritten.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip(" "))
                # Top-level keys (indent == 0)
                if indent == 0:
                    if stripped.startswith("services"):
                        current_section = "services"
                        continue
                    elif stripped.startswith("volumes"):
                        current_section = "volumes"
                        continue
                    elif stripped.startswith("networks"):
                        current_section = "networks"
                        continue
                    else:
                        current_section = "other"
                        continue

                if current_section == "services":
                    all_services_lines.append(line)
                elif current_section == "volumes":
                    vol_name = stripped.split(":")[0].strip()
                    if vol_name and vol_name not in seen_volumes:
                        seen_volumes.add(vol_name)
                        all_volumes_lines.append(line)
                elif current_section == "networks":
                    net_name = stripped.split(":")[0].strip()
                    if net_name and net_name not in seen_networks:
                        seen_networks.add(net_name)
                        all_networks_lines.append(line)

            messages.append(f"已合并子项目 {subdir} 的 docker-compose.yml")
        else:
            # Only has Dockerfile — generate a minimal service entry
            svc_name = subdir.replace("_", "-").replace(" ", "-").lower()
            all_services_lines.append(f"  {svc_name}:")
            all_services_lines.append(f"    build:")
            all_services_lines.append(f"      context: ./{subdir}")
            all_services_lines.append(f"      dockerfile: Dockerfile")
            all_services_lines.append(f"    restart: unless-stopped")
            all_services_lines.append("")
            messages.append(f"已为子项目 {subdir} 生成默认 service 配置")

    # Build merged compose
    parts = ["services:"]
    parts.extend(all_services_lines)
    parts.append("")

    if all_volumes_lines:
        parts.append("volumes:")
        parts.extend(all_volumes_lines)
        parts.append("")

    if all_networks_lines:
        parts.append("networks:")
        parts.extend(all_networks_lines)
        parts.append("")

    return "\n".join(parts), messages


def detect_project_type_for_path(path):
    """Return a project type and detection reason for a local source directory."""
    if not path or not os.path.isdir(path):
        return "", "invalid source directory"

    try:
        files = {item.lower() for item in os.listdir(path)}
    except Exception as exc:
        return "", str(exc)

    if any(name in files for name in ("requirements.txt", "pyproject.toml", "pipfile", "pipfile.lock")):
        return "python", "found Python dependency file"
    if any(name in files for name in ("setup.py", "manage.py", "app.py", "main.py", "wsgi.py", "asgi.py")):
        return "python", "found Python entrypoint"
    if any(name in files for name in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        return "java", "found Java build file"
    if "go.mod" in files:
        return "go", "found go.mod"
    if any(name.endswith(".csproj") or name.endswith(".sln") for name in files):
        return "dotnet", "found .NET project file"
    if "composer.json" in files:
        return "php", "found composer.json"
    if "package.json" in files:
        try:
            with open(os.path.join(path, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = {}
            deps.update(pkg.get("dependencies", {}))
            deps.update(pkg.get("devDependencies", {}))
            if any(dep in deps for dep in ("vue", "@vue/cli-service", "react", "react-dom", "next", "nuxt")):
                return "vue", "found frontend dependency"
        except Exception:
            pass
        return "nodejs", "found package.json"
    if "index.html" in files:
        return "static", "found index.html"

    # Check for monorepo: subdirectories with Dockerfile/docker-compose.yml
    mono_services = detect_monorepo_services(path)
    if mono_services:
        names = ", ".join(s["name"] for s in mono_services)
        return "monorepo", f"found sub-projects: {names}"

    return "", "no known project marker"


def default_deploy_templates(project_type, port=None):
    """Return fallback docker-compose and Dockerfile content for a project type."""
    defaults = {
        "python": (8000, "app-python"),
        "nodejs": (3000, "app-nodejs"),
        "vue": (80, "app-frontend"),
        "java": (8080, "app-java"),
        "go": (8080, "app-go"),
        "php": (80, "app-php"),
        "dotnet": (8080, "app-dotnet"),
        "static": (80, "app-static"),
    }
    default_port, container_name = defaults.get(project_type or "", (3000, "app-web"))
    try:
        port = int(port or default_port)
    except (TypeError, ValueError):
        port = default_port

    compose = f"""services:
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: {container_name}
    restart: unless-stopped
    ports:
      - "{port}:{port}"
    networks:
      - app-network

networks:
  app-network:
    driver: bridge
"""

    dockerfiles = {
        "python": f"""FROM python:3.12-slim

WORKDIR /app

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \\
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

EXPOSE {port}

CMD ["gunicorn", "-b", "0.0.0.0:{port}", "app:app"]
""",
        "nodejs": f"""FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm install --production=false

COPY . .

EXPOSE {port}

CMD ["npm", "start"]
""",
        "vue": """FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
""",
        "static": """FROM nginx:alpine

COPY . /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
""",
        "php": """FROM php:8.3-apache

WORKDIR /var/www/html
COPY . .
RUN docker-php-ext-install pdo_mysql mysqli
EXPOSE 80
""",
        "go": """FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/server ./main.go

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/server .
EXPOSE 8080
CMD ["./server"]
""",
        "java": """FROM eclipse-temurin:21-jdk-alpine AS builder
WORKDIR /app
COPY . .
RUN apk add --no-cache maven && mvn clean package -DskipTests

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
COPY --from=builder /app/target/*.jar app.jar
EXPOSE 8080
CMD ["java", "-jar", "app.jar"]
""",
        "dotnet": """FROM mcr.microsoft.com/dotnet/sdk:8.0 AS builder
WORKDIR /src
COPY . .
RUN dotnet publish -c Release -o /app/publish

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=builder /app/publish .
EXPOSE 8080
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "YourProject.dll"]
""",
    }
    return compose, dockerfiles.get(project_type or "", dockerfiles["nodejs"])


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "deploy_config.json")
DEPLOY_LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DEPLOY_LOG_DIR, exist_ok=True)

# 全局部署状态
deploy_status = {
    "running": False,
    "progress": 0,
    "message": "",
    "log": [],
    "success": False,
}
deploy_lock = threading.Lock()


def load_config():
    """加载配置"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"servers": [], "modules": [], "settings": {}}


def save_config(config):
    """保存配置"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def log_message(msg, level="info"):
    """添加日志"""
    with deploy_lock:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": msg,
        }
        deploy_status["log"].append(entry)


# ========================
#  Paramiko SSH 封装
# ========================

class SSHClient:
    """paramiko SSH 客户端封装"""

    def __init__(self, host, port, user, password=None, key_path=None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.key_path = key_path
        self.client = None

    def connect(self, timeout=15):
        """建立 SSH 连接"""
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }

        # 优先使用密钥
        expanded_key_path = os.path.expanduser(self.key_path) if self.key_path else ""
        has_key_path = bool(expanded_key_path and os.path.exists(expanded_key_path))

        if has_key_path:
            connect_kwargs["key_filename"] = expanded_key_path
        if self.password:
            connect_kwargs["password"] = self.password
        if not has_key_path and not self.password:
            connect_kwargs["allow_agent"] = True
            connect_kwargs["look_for_keys"] = True

        self.client.connect(**connect_kwargs)
        return True

    def exec_command(self, cmd, timeout=120, input_data=None, get_pty=False):
        """执行远程命令"""
        if not self.client:
            return -1, "", "SSH 未连接"
        try:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout, get_pty=get_pty)
            if input_data is not None:
                stdin.write(input_data)
                stdin.flush()
                stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            return exit_code, out, err
        except Exception as e:
            return -1, "", str(e)

    def exec_command_stream(self, cmd, timeout=1200, idle_timeout=300, on_stdout=None, on_stderr=None, get_pty=False):
        """执行远程命令并实时回调输出，避免长时间构建看起来卡死。"""
        if not self.client:
            return -1, "", "SSH 未连接"

        def emit_complete_lines(buffer, callback):
            lines = buffer.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                pending = lines.pop()
            else:
                pending = ""
            for line in lines:
                text = line.strip()
                if text and callback:
                    callback(text)
            return pending

        out_chunks = []
        err_chunks = []
        out_buffer = ""
        err_buffer = ""
        started_at = time.time()
        last_activity = started_at

        try:
            stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout, get_pty=get_pty)
            stdin.close()
            channel = stdout.channel

            while True:
                has_activity = False

                while channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    out_chunks.append(data)
                    out_buffer += data
                    out_buffer = emit_complete_lines(out_buffer, on_stdout)
                    has_activity = True

                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    err_chunks.append(data)
                    err_buffer += data
                    err_buffer = emit_complete_lines(err_buffer, on_stderr)
                    has_activity = True

                if has_activity:
                    last_activity = time.time()

                if channel.exit_status_ready():
                    while channel.recv_ready():
                        data = channel.recv(4096).decode("utf-8", errors="replace")
                        out_chunks.append(data)
                        out_buffer += data
                    while channel.recv_stderr_ready():
                        data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        err_chunks.append(data)
                        err_buffer += data
                    break

                now = time.time()
                if timeout and now - started_at > timeout:
                    channel.close()
                    return -1, "".join(out_chunks), "".join(err_chunks), f"命令执行超时（超过 {timeout} 秒）"
                if idle_timeout and now - last_activity > idle_timeout:
                    channel.close()
                    return -1, "".join(out_chunks), "".join(err_chunks), f"命令长时间无输出（超过 {idle_timeout} 秒）"

                time.sleep(0.2)

            if out_buffer.strip() and on_stdout:
                on_stdout(out_buffer.strip())
            if err_buffer.strip() and on_stderr:
                on_stderr(err_buffer.strip())

            exit_code = channel.recv_exit_status()
            return exit_code, "".join(out_chunks), "".join(err_chunks), ""
        except Exception as e:
            return -1, "".join(out_chunks), "".join(err_chunks), str(e)

    def prepare_remote_dir(self, remote_dir):
        """Create a writable remote deploy directory for the current SSH user."""
        remote_dir_q = shlex.quote(remote_dir)
        writable_check = f"test -d {remote_dir_q} && test -w {remote_dir_q}"
        plain_cmd = f"mkdir -p -- {remote_dir_q} && {writable_check}"

        rc, out, err = self.exec_command(plain_cmd)
        if rc == 0:
            return True, "plain", ""

        root_script = shlex.quote(
            'mkdir -p -- "$1" && chown -R "$2:$3" -- "$1" && chmod u+rwx -- "$1"'
        )
        sudo_cmd = (
            "owner=$(id -un) && group=$(id -gn) && "
            f"sudo -n sh -c {root_script} sh {remote_dir_q} \"$owner\" \"$group\" && "
            f"{writable_check}"
        )
        rc, sudo_out, sudo_err = self.exec_command(sudo_cmd)
        if rc == 0:
            return True, "sudo", ""

        if self.password:
            sudo_password_cmd = (
                "owner=$(id -un) && group=$(id -gn) && "
                f"sudo -S -p '' sh -c {root_script} sh {remote_dir_q} \"$owner\" \"$group\" && "
                f"{writable_check}"
            )
            rc, pass_out, pass_err = self.exec_command(
                sudo_password_cmd,
                input_data=f"{self.password}\n",
            )
            if rc == 0:
                return True, "sudo-password", ""
            sudo_out, sudo_err = pass_out, pass_err

        detail = (sudo_err or sudo_out or err or out).strip()
        return False, "failed", detail or "Unable to create writable remote directory"

    def _ensure_remote_dir(self, sftp, remote_dir):
        """递归创建远程目录（确保父目录存在）"""
        normalized = remote_dir.replace("\\", "/").rstrip("/")
        if not normalized:
            return

        parts = [part for part in normalized.split("/") if part]
        current = "/" if normalized.startswith("/") else ""
        for part in parts:
            current = current.rstrip("/") + "/" + part if current else part
            try:
                sftp.stat(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass  # 竞态条件：目录已被创建
            try:
                attrs = sftp.stat(current)
            except OSError as stat_error:
                raise RuntimeError(f"Failed to create remote directory: {current}; {stat_error}") from stat_error
            if not stat.S_ISDIR(getattr(attrs, "st_mode", 0)):
                raise RuntimeError(f"Remote path exists but is not a directory: {current}")

    def upload_dir(self, local_dir, remote_dir, excludes=None):
        """上传目录到远程（SFTP）"""
        if not self.client:
            return False, "SSH 未连接"

        # 验证本地源路径
        local_dir = os.path.abspath(local_dir)
        if not os.path.exists(local_dir):
            return False, f"本地路径不存在: {local_dir}"
        if not os.path.isdir(local_dir):
            return False, f"本地路径不是目录: {local_dir}"

        if excludes is None:
            excludes = []

        import traceback

        sftp = self.client.open_sftp()

        try:
            # Patterns that should only be excluded at the root level (depth 0)
            _root_only_excludes = {".env"}

            def _should_exclude(name, depth=0):
                name_lower = name.lower()
                for exc in excludes:
                    exc = exc.strip("/").lower()
                    if "*" in exc or "?" in exc:
                        if fnmatch.fnmatch(name_lower, exc):
                            return True
                    else:
                        if exc == name_lower or exc in name_lower:
                            # Some patterns (like .env) only apply at root level
                            if exc in _root_only_excludes and depth > 0:
                                continue
                            return True
                return False

            def _ensure_remote_dir(sftp_conn, rdir):
                """递归创建远程目录"""
                parts = rdir.replace("\\", "/").strip("/").split("/")
                current = ""
                for part in parts:
                    current = current + "/" + part
                    try:
                        sftp_conn.stat(current)
                    except IOError:
                        try:
                            sftp_conn.mkdir(current)
                        except IOError:
                            pass

            def _upload_recursive(local, remote, depth=0):
                self._ensure_remote_dir(sftp, remote)

                try:
                    items = os.listdir(local)
                except PermissionError:
                    return
                except OSError:
                    return

                for item in items:
                    if _should_exclude(item, depth):
                        continue

                    local_path = os.path.join(local, item)
                    remote_path = remote + "/" + item

                    if os.path.islink(local_path):
                        continue

                    if os.path.isdir(local_path):
                        _upload_recursive(local_path, remote_path, depth + 1)
                    elif os.path.isfile(local_path):
                        try:
                            sftp.put(local_path, remote_path)
                        except Exception as e:
                            import traceback
                            tb = traceback.format_exc()
                            raise RuntimeError(
                                f"上传文件失败: local={local_path!r}, remote={remote_path!r}\n"
                                f"Error: {e}\n{tb}"
                            ) from e

            _upload_recursive(local_dir, remote_dir)
            return True, ""

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            return False, f"{str(e)}\n\nTraceback:\n{tb}"

        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def upload_file(self, local_path, remote_path):
        """上传单个文件"""
        if not self.client:
            return False, "SSH 未连接"
        sftp = self.client.open_sftp()
        try:
            remote_parent = remote_path.replace("\\", "/").rsplit("/", 1)[0]
            if remote_parent:
                self._ensure_remote_dir(sftp, remote_parent)
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()
        return True, ""

    def write_remote_text(self, remote_path, content):
        """Write a UTF-8 text file over SFTP, creating parent directories first."""
        if not self.client:
            return False, "SSH 未连接"
        sftp = self.client.open_sftp()
        try:
            remote_parent = remote_path.replace("\\", "/").rsplit("/", 1)[0]
            if remote_parent:
                self._ensure_remote_dir(sftp, remote_parent)
            with sftp.file(remote_path, "w") as f:
                f.write(content)
            return True, ""
        except Exception as e:
            return False, str(e)
        finally:
            sftp.close()

    def ensure_path_gateway(self, project_slug, container_name, target_port, public_domain):
        """Create/update the shared HTTPS gateway route for a deployed container."""
        gateway_root = "/opt/aigenimage-gateway"
        gateway_network = "aigenimage_gateway"
        gateway_name = "aigenimage-gateway"
        route_path = f"/{project_slug}"
        domain, server_names, cert_base = gateway_domain_values(public_domain)
        use_https = not is_ip_address(domain)

        for path in (gateway_root, f"{gateway_root}/conf.d", f"{gateway_root}/routes"):
            ok, _, error = self.prepare_remote_dir(path)
            if not ok:
                return False, f"无法准备网关目录 {path}: {error}"

        if use_https:
            main_conf = f"""server {{
    listen 80;
    server_name {server_names};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {server_names};

    ssl_certificate /etc/nginx/ssl/{cert_base}_bundle.crt;
    ssl_certificate_key /etc/nginx/ssl/{cert_base}.key;

    client_max_body_size 100m;

    include /etc/nginx/routes/*.conf;

    location = / {{
        default_type text/plain;
        return 200 "aigenimage deployment gateway\\n";
    }}
}}
"""
            access_url = f"https://{domain}/{project_slug}/"
        else:
            main_conf = f"""server {{
    listen 80;
    server_name {server_names};

    client_max_body_size 100m;

    include /etc/nginx/routes/*.conf;

    location = / {{
        default_type text/plain;
        return 200 "aigenimage deployment gateway\\n";
    }}
}}
"""
            access_url = f"http://{domain}/{project_slug}/"
        route_conf = f"""location = {route_path} {{
    return 301 {route_path}/;
}}

location {route_path}/ {{
    proxy_pass http://{container_name}:{target_port}/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix {route_path};
    proxy_redirect off;
    proxy_buffering off;
    proxy_connect_timeout 60s;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    # 禁止网关缓存，确保前端更新即时生效
    add_header Cache-Control "no-cache, no-store, must-revalidate" always;
    add_header Pragma "no-cache" always;
    add_header Expires "0" always;
}}
"""

        for remote_path, content in (
            (f"{gateway_root}/conf.d/default.conf", main_conf),
            (f"{gateway_root}/routes/{project_slug}.conf", route_conf),
        ):
            ok, error = self.write_remote_text(remote_path, content)
            if not ok:
                return False, f"写入网关配置失败 {remote_path}: {error}"

        container_q = shlex.quote(container_name)
        network_q = shlex.quote(gateway_network)
        self.exec_command(f"docker network create {network_q} >/dev/null 2>&1 || true", timeout=30)
        self.exec_command(
            f"docker network inspect {network_q} --format '{{{{range .Containers}}}}{{{{.Name}}}} {{{{end}}}}' "
            f"| grep -qw {container_q} || docker network connect {network_q} {container_q}",
            timeout=30,
        )

        test_cmd = (
            f"docker run --rm --network {network_q} "
            f"-v {gateway_root}/conf.d:/etc/nginx/conf.d:ro "
            f"-v {gateway_root}/routes:/etc/nginx/routes:ro "
            f"-v /etc/nginx/ssl:/etc/nginx/ssl:ro "
            "nginx:latest nginx -t"
        )
        rc, out, err = self.exec_command(test_cmd, timeout=60)
        if rc != 0:
            return False, f"Nginx 配置校验失败: {err or out}"

        exists_cmd = f"docker ps -a --format '{{{{.Names}}}}' | grep -Fx {shlex.quote(gateway_name)}"
        rc, _, _ = self.exec_command(exists_cmd, timeout=10)
        if rc == 0:
            reload_cmd = f"docker exec {gateway_name} nginx -t && docker exec {gateway_name} nginx -s reload"
            rc, out, err = self.exec_command(reload_cmd, timeout=30)
            if rc != 0:
                return False, f"网关重载失败: {err or out}"
        else:
            run_cmd = (
                f"docker run -d --name {gateway_name} --restart unless-stopped "
                f"--network {network_q} "
                "-p 80:80 -p 443:443 "
                f"-v {gateway_root}/conf.d:/etc/nginx/conf.d:ro "
                f"-v {gateway_root}/routes:/etc/nginx/routes:ro "
                "-v /etc/nginx/ssl:/etc/nginx/ssl:ro "
                "nginx:latest"
            )
            rc, out, err = self.exec_command(run_cmd, timeout=60)
            if rc != 0:
                return False, f"网关启动失败: {err or out}"

        return True, access_url

    def close(self):
        """关闭连接"""
        if self.client:
            self.client.close()
            self.client = None


# ========================
#  API 路由
# ========================


@app.route("/")
def index():
    """主页"""
    response = render_template("index.html")
    resp = app.make_response(response)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取配置"""
    config = load_config()
    safe_config = {"servers": [], "modules": config.get("modules", []), "settings": config.get("settings", {})}
    for srv in config.get("servers", []):
        safe_srv = dict(srv)
        if "password" in safe_srv:
            safe_srv["password"] = "***" if safe_srv["password"] else ""
        safe_config["servers"].append(safe_srv)
    return jsonify(safe_config)


@app.route("/api/config/servers", methods=["POST"])
def add_server():
    """添加/更新服务器配置"""
    data = request.json
    config = load_config()
    if "servers" not in config:
        config["servers"] = []

    found = False
    for i, srv in enumerate(config["servers"]):
        if srv.get("name") == data.get("name"):
            if data.get("password") in ("", "***", None) and srv.get("password"):
                data["password"] = srv["password"]
            config["servers"][i] = data
            found = True
            break

    if not found:
        config["servers"].append(data)

    save_config(config)
    return jsonify({"success": True, "message": "服务器配置已保存"})


@app.route("/api/config/servers/<name>", methods=["DELETE"])
def delete_server(name):
    """删除服务器配置"""
    config = load_config()
    config["servers"] = [s for s in config.get("servers", []) if s.get("name") != name]
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/config/modules", methods=["POST"])
def add_module():
    """添加/更新模块配置"""
    data = request.json
    config = load_config()
    if "modules" not in config:
        config["modules"] = []

    found = False
    for i, mod in enumerate(config["modules"]):
        if mod.get("name") == data.get("name"):
            config["modules"][i] = data
            found = True
            break

    if not found:
        config["modules"].append(data)

    save_config(config)
    return jsonify({"success": True, "message": "模块配置已保存"})


@app.route("/api/config/modules/<name>", methods=["DELETE"])
def delete_module(name):
    """删除模块配置"""
    config = load_config()
    config["modules"] = [m for m in config.get("modules", []) if m.get("name") != name]
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/config/settings", methods=["POST"])
def update_settings():
    """更新全局设置"""
    data = request.json
    config = load_config()
    config["settings"] = data
    save_config(config)
    return jsonify({"success": True})


@app.route("/api/qrcode", methods=["GET"])
def generate_qrcode():
    """生成访问地址二维码 SVG。"""
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少 url 参数"}), 400
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return jsonify({"error": "仅支持 http/https 网址"}), 400

    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return jsonify({"error": "缺少 qrcode 依赖，请先执行 pip install -r requirements.txt"}), 500

    image = qrcode.make(
        url,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
        border=4,
    )
    output = io.BytesIO()
    image.save(output)

    response = Response(output.getvalue(), mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/api/browse", methods=["POST"])
def browse_directory():
    """浏览目录（返回子目录列表）"""
    data = request.json
    path = data.get("path", "")

    if not path:
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(f"{letter}:\\"):
                drives.append({"name": f"{letter}:", "path": f"{letter}:\\"})
        return jsonify({"drives": drives, "items": []})

    items = []
    try:
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            is_dir = os.path.isdir(full_path)
            if is_dir:
                items.append({"name": item, "path": full_path, "type": "dir"})
            else:
                items.append({"name": item, "path": full_path, "type": "file"})
    except PermissionError:
        return jsonify({"error": "权限不足，无法访问此目录"})
    except Exception as e:
        return jsonify({"error": str(e)})

    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return jsonify({"items": items})


@app.route("/api/detect-project-type", methods=["POST"])
def detect_project_type():
    """自动检测项目类型"""
    data = request.json
    path = data.get("path", "")

    if not path or not os.path.isdir(path):
        return jsonify({"type": "", "reason": "路径不存在"})

    files = set()
    try:
        for item in os.listdir(path):
            files.add(item.lower())
    except Exception:
        return jsonify({"type": "", "reason": "无法读取目录"})

    if "requirements.txt" in files:
        return jsonify({"type": "python", "reason": "找到 requirements.txt"})
    if "pyproject.toml" in files:
        return jsonify({"type": "python", "reason": "找到 pyproject.toml"})
    if "pipfile" in files or "pipfile.lock" in files:
        return jsonify({"type": "python", "reason": "找到 Pipfile"})
    if "setup.py" in files or "manage.py" in files:
        return jsonify({"type": "python", "reason": "找到 setup.py/manage.py"})
    if "app.py" in files or "main.py" in files or "wsgi.py" in files or "asgi.py" in files:
        return jsonify({"type": "python", "reason": "找到 app.py/main.py"})

    if "pom.xml" in files:
        return jsonify({"type": "java", "reason": "找到 pom.xml"})
    if "build.gradle" in files or "build.gradle.kts" in files:
        return jsonify({"type": "java", "reason": "找到 build.gradle"})
    if "settings.gradle" in files or "settings.gradle.kts" in files:
        return jsonify({"type": "java", "reason": "找到 settings.gradle"})

    if "go.mod" in files:
        return jsonify({"type": "go", "reason": "找到 go.mod"})

    for f in files:
        if f.endswith(".csproj"):
            return jsonify({"type": "dotnet", "reason": f"找到 {f}"})
        if f.endswith(".sln"):
            return jsonify({"type": "dotnet", "reason": f"找到 {f}"})

    if "composer.json" in files:
        return jsonify({"type": "php", "reason": "找到 composer.json"})

    if "package.json" in files:
        try:
            with open(os.path.join(path, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = {}
            deps.update(pkg.get("dependencies", {}))
            deps.update(pkg.get("devDependencies", {}))
            scripts = pkg.get("scripts", {})

            if "vue" in deps or "@vue/cli-service" in deps:
                return jsonify({"type": "vue", "reason": "检测到 Vue 项目"})
            if "react" in deps or "react-dom" in deps or "next" in deps:
                return jsonify({"type": "vue", "reason": "检测到 React 项目"})
            if "create-react-app" in deps or "react-scripts" in deps:
                return jsonify({"type": "vue", "reason": "检测到 React 项目"})
            if "nuxt" in deps or "@nuxt/cli" in deps:
                return jsonify({"type": "vue", "reason": "检测到 Nuxt 项目"})
            if "build" in scripts:
                return jsonify({"type": "nodejs", "reason": "检测到 Node.js 项目"})
            return jsonify({"type": "nodejs", "reason": "找到 package.json"})
        except Exception:
            return jsonify({"type": "nodejs", "reason": "找到 package.json"})

    if "index.html" in files:
        return jsonify({"type": "static", "reason": "找到 index.html"})

    # Check for monorepo: subdirectories with Dockerfile/docker-compose.yml
    mono_services = detect_monorepo_services(path)
    if mono_services:
        names = ", ".join(s["name"] for s in mono_services)
        return jsonify({"type": "monorepo", "reason": f"检测到前后端分离项目: {names}"})

    return jsonify({"type": "", "reason": "无法识别项目类型"})


@app.route("/api/ssh-test", methods=["POST"])
def test_ssh():
    """测试 SSH 连接（使用 paramiko）"""
    data = request.json
    name = data.get("name", "")
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")

    if name and password in ("", "***", None):
        config = load_config()
        server_config = next((s for s in config.get("servers", []) if s.get("name") == name), None)
        if server_config:
            host = server_config.get("host", host)
            port = server_config.get("port", port)
            user = server_config.get("user", user)
            password = server_config.get("password", password)
            key_path = server_config.get("key_path", key_path)

    if not host or not user:
        return jsonify({"success": False, "message": "主机和用户名不能为空"})

    try:
        ssh = SSHClient(host, int(port), user, password=password, key_path=key_path)
        ssh.connect(timeout=15)
        rc, out, err = ssh.exec_command("echo OK", timeout=10)
        ssh.close()
        if rc == 0 and "OK" in out:
            return jsonify({"success": True, "message": "SSH 连接成功！"})
        else:
            return jsonify({"success": False, "message": err.strip() or "命令执行失败"})
    except paramiko.AuthenticationException:
        return jsonify({"success": False, "message": "认证失败：用户名或密码错误"})
    except paramiko.SSHException as e:
        return jsonify({"success": False, "message": f"SSH 错误: {str(e)}"})
    except Exception as e:
        return jsonify({"success": False, "message": f"连接失败: {str(e)}"})


@app.route("/api/deploy", methods=["POST"])
def deploy():
    """执行部署"""
    with deploy_lock:
        if deploy_status["running"]:
            return jsonify({"success": False, "message": "已有部署任务正在运行"})

        data = request.json
        server_name = data.get("server_name", "")
        module_name = data.get("module_name", "")
        source_path = data.get("source_path", "")
        server_path = data.get("server_path", "")

        if not all([server_name, module_name, source_path, server_path]):
            return jsonify({"success": False, "message": "请填写完整的部署信息"})

        config = load_config()
        server_config = next((s for s in config.get("servers", []) if s.get("name") == server_name), None)
        if not server_config:
            return jsonify({"success": False, "message": "服务器配置不存在"})

        deploy_data = dict(data)
        for field in ("host", "port", "user", "password", "key_path"):
            deploy_data[field] = server_config.get(field, "")
        deploy_data["server_path"] = server_config.get("deploy_path") or server_path

        deploy_status["running"] = True
        deploy_status["progress"] = 0
        deploy_status["message"] = "准备部署..."
        deploy_status["log"] = []
        deploy_status["success"] = False

        thread = threading.Thread(
            target=run_deploy,
            args=(server_name, module_name, source_path, deploy_data["server_path"], deploy_data),
        )
        thread.daemon = True
        thread.start()

        return jsonify({"success": True, "message": "部署任务已启动"})


def run_deploy(server_name, module_name, source_path, server_path, data):
    """执行部署流程（使用 paramiko）"""
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")
    config = load_config()
    settings = config.get("settings", {})
    public_domain = normalize_public_domain(data.get("public_domain") or settings.get("public_domain") or settings.get("domain"))

    ssh = None

    def set_progress(pct, msg):
        with deploy_lock:
            deploy_status["progress"] = pct
            deploy_status["message"] = msg

    def get_positive_int(value, default):
        try:
            number = int(value)
            return number if number > 0 else default
        except (TypeError, ValueError):
            return default

    build_timeout = get_positive_int(settings.get("build_timeout_seconds"), 1800)
    build_idle_timeout = get_positive_int(settings.get("build_idle_timeout_seconds"), 300)

    try:
        # Step 0: 验证本地源路径
        source_path = os.path.abspath(source_path)
        if not os.path.exists(source_path):
            set_progress(0, "本地源路径不存在")
            log_message(f"本地路径不存在: {source_path}", "error")
            return
        if not os.path.isdir(source_path):
            set_progress(0, "本地源路径不是目录")
            log_message(f"本地路径不是目录: {source_path}", "error")
            return
        log_message(f"本地源目录: {source_path}")

        # Step 1: SSH 连接 (10%)
        set_progress(10, "连接服务器...")
        log_message(f"连接到 {user}@{host}:{port}")
        ssh = SSHClient(host, int(port), user, password=password, key_path=key_path)
        ssh.connect(timeout=15)
        rc, out, err = ssh.exec_command("echo SSH_OK", timeout=10)
        if rc != 0:
            set_progress(0, "SSH 连接失败")
            log_message(f"SSH 连接失败: {err}", "error")
            return
        log_message("SSH 连接成功")

        # Step 2: 检查 Docker (20%)
        set_progress(20, "检查远程 Docker...")
        rc, out, err = ssh.exec_command("docker --version")
        if rc != 0:
            set_progress(0, "远程服务器未安装 Docker")
            log_message("远程服务器未安装 Docker", "error")
            return
        log_message(f"远程 Docker: {out.strip()}")

        # Step 3: 检查 Docker Compose (30%)
        set_progress(30, "检查 Docker Compose...")
        rc, out, err = ssh.exec_command("docker compose version")
        compose_prefix = "docker compose"
        if rc != 0:
            rc2, out2, err2 = ssh.exec_command("docker-compose --version")
            if rc2 == 0:
                compose_prefix = "docker-compose"
            else:
                log_message("未检测到 Docker Compose，尝试安装...", "warn")
                ssh.exec_command(
                    "sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose && sudo chmod +x /usr/local/bin/docker-compose",
                    timeout=120,
                )
                compose_prefix = "docker-compose"
        log_message(f"Docker Compose: {compose_prefix}")

        # Step 4: 创建远程目录 (40%)
        set_progress(40, "创建远程目录...")
        remote_dir = f"{server_path}/{module_name}"
        remote_dir_q = shlex.quote(remote_dir)
        project_slug = slugify_project_name(os.path.basename(source_path))
        ok, create_mode, create_error = ssh.prepare_remote_dir(remote_dir)
        if not ok:
            log_message(f"创建远程目录失败: {create_error}", "error")
            set_progress(0, "创建远程目录失败")
            return
        if create_mode != "plain":
            log_message("远程目录需要 sudo 初始化，已授权给当前 SSH 用户", "warn")
        log_message(f"远程目录: {remote_dir}")

        # 保护已有 SQLite 数据：首次改为持久化挂载时，把旧容器内数据库复制到远程 data/。
        # 只在目标 data/data.db 不存在时执行，避免覆盖线上新数据。
        expected_container_name = f"{project_slug}-app"
        preserve_db_cmd = (
            f"mkdir -p {shlex.quote(remote_dir + '/data')}; "
            f"if [ ! -f {shlex.quote(remote_dir + '/data/data.db')} ]; then "
            f"if docker ps -a --format '{{{{.Names}}}}' | grep -Fx {shlex.quote(expected_container_name)} >/dev/null 2>&1; then "
            "for db_path in /app/data/data.db /app/backend/data.db /app/data.db; do "
            f"if docker exec {shlex.quote(expected_container_name)} test -f \"$db_path\" >/dev/null 2>&1; then "
            f"docker cp {shlex.quote(expected_container_name)}:\"$db_path\" {shlex.quote(remote_dir + '/data/data.db')} "
            "&& echo preserved:$db_path; break; "
            "fi; "
            "done; "
            "fi; "
            "fi"
        )
        rc, preserve_out, preserve_err = ssh.exec_command(preserve_db_cmd, timeout=60)
        if preserve_out.strip():
            log_message(f"已保护旧容器 SQLite 数据: {preserve_out.strip()}")
        if rc != 0 and preserve_err.strip():
            log_message(f"旧 SQLite 数据保护检查失败，将继续部署: {preserve_err.strip()}", "warn")

        # Step 5: 同步文件 (50-70%) — 使用 SFTP
        set_progress(50, "同步文件到服务器...")
        log_message("开始同步文件（SFTP）...")

        excludes = [
            ".git",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "node_modules",
            ".env",
            "__pycache__",
            ".claude",
            ".vscode",
            ".idea",
            ".deploy-logs",
            ".deploy-ssh",
            "logs",
            "data.db",
            "data.db-shm",
            "data.db-wal",
            "*.sqlite",
            "*.sqlite3",
            "*.db-shm",
            "*.db-wal",
            "*.log",
            "*.pyc",
            ".DS_Store",
            "Thumbs.db",
        ]

        success, err_msg = ssh.upload_dir(source_path, remote_dir, excludes=excludes)
        if success:
            log_message("文件同步完成（SFTP）")
        else:
            log_message(f"文件同步失败: {err_msg}", "error")
            set_progress(0, "文件同步失败")
            return

        set_progress(70, "文件同步完成")

        # Step 6: 写入 docker-compose.yml (80%)
        set_progress(80, "生成部署配置...")
        module_config = next((m for m in config.get("modules", []) if m.get("name") == module_name), {})
        project_type = (data.get("project_type") or module_config.get("project_type") or "").strip()
        if not project_type:
            project_type, detect_reason = detect_project_type_for_path(source_path)
            if project_type:
                log_message(f"自动识别项目类型: {project_type} ({detect_reason})")
        if project_type and module_config.get("project_type") != project_type:
            module_config["project_type"] = project_type
            save_config(config)

        compose_content = data.get("docker_compose", "")
        dockerfile_content = data.get("dockerfile", "")

        # 优先使用项目自带的 docker-compose.yml / Dockerfile。
        # 项目内的部署文件通常包含 volume、env_file 等运行时数据保护配置，
        # 比 Web 页面默认模板更准确。
        local_compose = os.path.join(source_path, "docker-compose.yml")
        if os.path.isfile(local_compose):
            with open(local_compose, "r", encoding="utf-8") as f:
                compose_content = f.read()
            log_message("使用项目自带的 docker-compose.yml")

        if project_type == "monorepo" and os.path.isfile(local_compose):
            with open(local_compose, "r", encoding="utf-8") as f:
                compose_content = f.read()
            log_message("monorepo 项目优先使用项目自带的 docker-compose.yml")

        local_df = os.path.join(source_path, "Dockerfile")
        if os.path.isfile(local_df):
            with open(local_df, "r", encoding="utf-8") as f:
                dockerfile_content = f.read()
            log_message("使用项目自带的 Dockerfile")

        # Only use templates if still no content
        if project_type == "monorepo" and not compose_content:
            # Monorepo: merge sub-project compose files
            mono_services = detect_monorepo_services(source_path)
            if mono_services:
                compose_content, mono_messages = merge_monorepo_compose(mono_services, source_path)
                for message in mono_messages:
                    log_message(message)
                if not compose_content.strip():
                    compose_content = ""
                    log_message("monorepo 合并结果为空，将使用默认模板", "warn")
            else:
                log_message("未检测到 monorepo 子项目", "warn")

        needs_root_dockerfile = project_type != "monorepo"
        if not compose_content or (needs_root_dockerfile and not dockerfile_content):
            default_compose, default_dockerfile = default_deploy_templates(project_type, module_config.get("port"))
            if not compose_content:
                compose_content = default_compose
                log_message("未提供 docker-compose.yml，已使用默认模板", "warn")
            if needs_root_dockerfile and not dockerfile_content:
                dockerfile_content = default_dockerfile
                log_message("未提供 Dockerfile，已使用默认模板", "warn")

        fallback_container_port = extract_first_container_port(compose_content)
        if compose_content:
            compose_content, ports_changed = normalize_compose_for_path_gateway(compose_content, project_slug)
            if ports_changed:
                log_message("已切换为路径网关模式：移除宿主机端口映射，改用容器内部 expose")
            # 通过 SFTP 写入文件
            sftp = ssh.client.open_sftp()
            with sftp.file(f"{remote_dir}/docker-compose.yml", "w") as f:
                f.write(compose_content)
            log_message("docker-compose.yml 已写入")

        # 写入 Dockerfile（monorepo 不需要根目录 Dockerfile，各子项目有自己的）
        if dockerfile_content and project_type != "monorepo":
            dockerfile_content, node_messages = normalize_dockerfile_for_node_package(dockerfile_content, source_path)
            for message in node_messages:
                log_message(message)
            dockerfile_content, dockerfile_changed = normalize_dockerfile_for_python_gateway(dockerfile_content)
            if dockerfile_changed:
                log_message("已为 Python gunicorn 模板补充 gunicorn 依赖")
            sftp = ssh.client.open_sftp()
            with sftp.file(f"{remote_dir}/Dockerfile", "w") as f:
                f.write(dockerfile_content)
            log_message("Dockerfile 已写入")

        # 写入 .env 文件（环境变量）— 自动从本地项目目录读取
        local_env_path = os.path.join(source_path, ".env")
        env_vars_content = ""
        if os.path.isfile(local_env_path):
            with open(local_env_path, "r", encoding="utf-8", errors="replace") as f:
                env_vars_content = f.read().strip()
            log_message("已读取本地 .env 文件")
        if env_vars_content:
            sftp = ssh.client.open_sftp()
            with sftp.file(f"{remote_dir}/.env", "w") as f:
                f.write(env_vars_content)
            log_message(".env 环境变量文件已写入")

        # Step 7: 远程 Docker 部署 (90%)
        set_progress(90, "远程构建和部署...")
        compose_project_flag = f"-p {shlex.quote(project_slug)}"

        def run_streamed_step(title, command, timeout, idle_timeout=None):
            log_message(title)
            rc, out, err, stream_error = ssh.exec_command_stream(
                command,
                timeout=timeout,
                idle_timeout=idle_timeout,
                on_stdout=lambda line: log_message(line),
                on_stderr=lambda line: log_message(line),
            )
            if stream_error:
                log_message(stream_error, "error")
            return rc, out, err

        pull_cmd = f"cd -- {remote_dir_q} && {compose_prefix} {compose_project_flag} -f docker-compose.yml pull"
        rc, out, err = run_streamed_step("执行 docker compose pull...", pull_cmd, timeout=300, idle_timeout=180)
        if rc != 0:
            log_message("docker compose pull 未完成，将继续尝试本地构建", "warn")

        build_progress_arg = " --progress=plain" if compose_prefix == "docker compose" else ""
        build_cmd = f"cd -- {remote_dir_q} && {compose_prefix} {compose_project_flag} -f docker-compose.yml build{build_progress_arg}"
        rc, out, err = run_streamed_step(
            f"执行 docker compose build...（最长等待 {build_timeout} 秒，{build_idle_timeout} 秒无输出则中止）",
            build_cmd,
            timeout=build_timeout,
            idle_timeout=build_idle_timeout,
        )
        if rc != 0:
            set_progress(0, "Docker 构建失败")
            detail = (err or out).strip()
            if detail:
                log_message(detail[-1000:], "error")
            log_message("Docker 构建失败，已停止部署", "error")
            return

        log_message("执行 docker compose up -d...")
        expected_container_q = shlex.quote(expected_container_name)
        cleanup_conflict_cmd = (
            "existing_id=$(docker ps -aq --filter "
            f"{shlex.quote(f'name=^/{expected_container_name}$')} | head -n 1); "
            "if [ -n \"$existing_id\" ]; then "
            "existing_project=$(docker inspect -f '{{ index .Config.Labels \"com.docker.compose.project\" }}' \"$existing_id\" 2>/dev/null || true); "
            f"if [ \"$existing_project\" != {shlex.quote(project_slug)} ]; then "
            "docker rm -f \"$existing_id\" >/dev/null && "
            f"echo removed:{expected_container_q}:$existing_project; "
            "fi; "
            "fi"
        )
        rc, cleanup_out, cleanup_err = ssh.exec_command(cleanup_conflict_cmd, timeout=60)
        if cleanup_out.strip():
            log_message(f"已清理历史同名容器: {cleanup_out.strip()}", "warn")
        if rc != 0 and cleanup_err.strip():
            log_message(f"历史同名容器检查失败，将继续尝试启动: {cleanup_err.strip()}", "warn")

        up_cmd = f"cd -- {remote_dir_q} && {compose_prefix} {compose_project_flag} -f docker-compose.yml up -d --remove-orphans"
        rc, out, err, stream_error = ssh.exec_command_stream(
            up_cmd,
            timeout=300,
            idle_timeout=180,
            on_stdout=lambda line: log_message(line),
            on_stderr=lambda line: log_message(line),
        )
        if stream_error:
            log_message(stream_error, "error")
        if rc != 0 and "is already in use" in (err or out):
            log_message(f"检测到同名容器冲突，正在移除 {expected_container_name} 后重试...", "warn")
            ssh.exec_command(f"docker rm -f {expected_container_q}", timeout=60)
            rc, out, err, stream_error = ssh.exec_command_stream(
                up_cmd,
                timeout=120,
                idle_timeout=90,
                on_stdout=lambda line: log_message(line),
                on_stderr=lambda line: log_message(line),
            )
            if stream_error:
                log_message(stream_error, "error")
        if rc == 0:
            log_message("容器启动成功！")
        else:
            set_progress(0, "容器启动失败")
            log_message(f"容器启动失败: {err or out}", "error")
            return

        rc, container_id, err = ssh.exec_command(
            f"cd -- {remote_dir_q} && {compose_prefix} {compose_project_flag} -f docker-compose.yml ps -q",
            timeout=30,
        )
        all_container_ids = [cid.strip() for cid in container_id.strip().split("\n") if cid.strip()]
        if not all_container_ids:
            set_progress(0, "容器信息获取失败")
            log_message(f"容器信息获取失败: {err}", "error")
            return

        # For monorepo: inspect all containers and prefer one exposing port 80 (frontend)
        # For single-service: just use the first container
        container_name = ""
        target_port = ""
        fallback_container_name = ""
        fallback_target_port = ""
        preferred_services = {"frontend", "web", "app", "backend", "api", "server"}
        infrastructure_services = {"db", "database", "postgres", "postgresql", "mysql", "redis", "mongo", "mongodb"}

        for cid in all_container_ids:
            inspect_cmd = (
                "docker inspect --format '{{.Name}} {{index .Config.Labels \"com.docker.compose.service\"}} {{range $p, $_ := .Config.ExposedPorts}}{{$p}} {{end}}' "
                f"{shlex.quote(cid)}"
            )
            rc, inspect_out, inspect_err = ssh.exec_command(inspect_cmd, timeout=30)
            if rc != 0:
                continue

            parts = inspect_out.strip().split()
            c_name = parts[0].lstrip("/") if parts else ""
            c_service = parts[1] if len(parts) > 1 and "/" not in parts[1] else ""
            port_parts = parts[2:] if c_service else parts[1:]
            c_ports = [part.split("/")[0] for part in port_parts if "/" in part]

            if not c_name:
                continue

            if c_service in infrastructure_services:
                if not fallback_container_name and c_ports:
                    fallback_container_name = c_name
                    fallback_target_port = c_ports[0]
                continue

            # Prefer app-like containers exposing port 80 (typical frontend/nginx)
            if "80" in c_ports and (not c_service or c_service in preferred_services):
                container_name = c_name
                target_port = "80"
                break

            if c_service in preferred_services and c_ports:
                container_name = c_name
                target_port = c_ports[0]
                continue

            # Fallback: use first non-infrastructure container with any exposed port
            if not container_name and c_ports:
                container_name = c_name
                target_port = c_ports[0]

        if not container_name and fallback_container_name:
            container_name = fallback_container_name
            target_port = fallback_target_port

        if not container_name:
            container_name = all_container_ids[0]
            target_port = fallback_container_port

        if not container_name or not target_port:
            set_progress(0, "路径网关配置失败")
            log_message("路径网关配置失败: 无法识别容器名或内部端口", "error")
            return

        if project_type == "monorepo":
            log_message(f"网关路由到容器: {container_name}:{target_port}")

        ok, gateway_result = ssh.ensure_path_gateway(project_slug, container_name, target_port, public_domain)
        if not ok:
            set_progress(0, "路径网关配置失败")
            log_message(f"路径网关配置失败: {gateway_result}", "error")
            return
        log_message(f"访问地址: {gateway_result}")

        # Step 8: 显示状态 (100%)
        set_progress(100, "部署完成！")
        log_message("获取容器状态...")
        rc, out, err = ssh.exec_command(f"cd -- {remote_dir_q} && {compose_prefix} {compose_project_flag} -f docker-compose.yml ps")
        if out:
            for line in out.strip().split("\n"):
                if line.strip():
                    log_message(line.strip())

        with deploy_lock:
            deploy_status["success"] = True

    except paramiko.AuthenticationException:
        log_message("认证失败：用户名或密码错误", "error")
        with deploy_lock:
            deploy_status["success"] = False
    except paramiko.SSHException as e:
        log_message(f"SSH 错误: {str(e)}", "error")
        with deploy_lock:
            deploy_status["success"] = False
    except Exception as e:
        log_message(f"部署出错: {str(e)}", "error")
        with deploy_lock:
            deploy_status["success"] = False
    finally:
        if ssh:
            ssh.close()
        with deploy_lock:
            deploy_status["running"] = False


@app.route("/api/deploy/status", methods=["GET"])
def get_deploy_status():
    """获取部署状态"""
    with deploy_lock:
        return jsonify({
            "running": deploy_status["running"],
            "progress": deploy_status["progress"],
            "message": deploy_status["message"],
            "log": deploy_status["log"],
            "success": deploy_status["success"],
        })


@app.route("/api/logs", methods=["GET"])
def get_logs():
    """获取部署日志"""
    log_files = []
    if os.path.exists(DEPLOY_LOG_DIR):
        for f in os.listdir(DEPLOY_LOG_DIR):
            if f.endswith(".log"):
                full_path = os.path.join(DEPLOY_LOG_DIR, f)
                log_files.append({"name": f, "path": full_path, "size": os.path.getsize(full_path)})
    return jsonify({"logs": log_files})


@app.route("/api/logs/<filename>", methods=["GET"])
def get_log_content(filename):
    """获取日志内容"""
    filepath = os.path.join(DEPLOY_LOG_DIR, filename)
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return jsonify({"content": f.read()})
    return jsonify({"error": "日志文件不存在"}), 404


if __name__ == "__main__":
    print("""
  _____   ____  _____  _______ ______ _____
 |  __ \\ / __ \\|  __ \\|__   __|  ____/ ____|
 | |  | | |  | | |__) |  | |  | |__ | |
 | |  | | |  | |  _  /   | |  |  __|| |
 | |__| | |__| | | \\ \\   | |  | |___| |____
 |_____/ \\____/|_|  \\_\\  |_|  |______\\_____|

  一键部署 - Web 可视化部署工具
  http://localhost:5001
""")
    app.run(host="0.0.0.0", port=5001, debug=False)
