"""
一键部署 - Web 可视化部署工具
Flask 后端服务
"""
import json
import os
import sys
import subprocess
import threading
import tempfile
import base64
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# 配置
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


def run_command(cmd, cwd=None, timeout=300):
    """运行命令并返回输出"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except Exception as e:
        return -1, "", str(e)


def generate_docker_compose(module_name, port=3000):
    """生成 docker-compose.yml"""
    return f"""version: "3.8"

services:
  {module_name}:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: app-{module_name}
    restart: unless-stopped
    ports:
      - "{port}:{port}"
    environment:
      - NODE_ENV=production
    networks:
      - app-network

networks:
  app-network:
    driver: bridge
"""


def generate_dockerfile():
    """生成 Dockerfile"""
    return """FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci --production=false

COPY . .
RUN npm run build

EXPOSE 3000

CMD ["node", "dist/index.js"]
"""


# ========================
#  API 路由
# ========================


@app.route("/")
def index():
    """主页"""
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    """获取配置"""
    config = load_config()
    # 返回脱敏后的配置
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

    # 查找是否已存在
    found = False
    for i, srv in enumerate(config["servers"]):
        if srv.get("name") == data.get("name"):
            # 保留旧密码（如果新密码为空）
            if not data.get("password") and srv.get("password"):
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


@app.route("/api/browse", methods=["POST"])
def browse_directory():
    """浏览目录（返回子目录列表）"""
    data = request.json
    path = data.get("path", "")

    # 如果路径为空，返回常见根目录
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

    # 排序：目录在前
    items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))

    return jsonify({"items": items})


@app.route("/api/detect-project-type", methods=["POST"])
def detect_project_type():
    """自动检测项目类型"""
    data = request.json
    path = data.get("path", "")

    if not path or not os.path.isdir(path):
        return jsonify({"type": "", "reason": "路径不存在"})

    # 检测规则
    files = set()
    try:
        for item in os.listdir(path):
            files.add(item.lower())
    except Exception:
        return jsonify({"type": "", "reason": "无法读取目录"})

    # Python: requirements.txt, pyproject.toml, Pipfile, setup.py
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

    # Java: pom.xml, build.gradle
    if "pom.xml" in files:
        return jsonify({"type": "java", "reason": "找到 pom.xml"})
    if "build.gradle" in files or "build.gradle.kts" in files:
        return jsonify({"type": "java", "reason": "找到 build.gradle"})
    if "settings.gradle" in files or "settings.gradle.kts" in files:
        return jsonify({"type": "java", "reason": "找到 settings.gradle"})

    # Go: go.mod
    if "go.mod" in files:
        return jsonify({"type": "go", "reason": "找到 go.mod"})

    # .NET: *.csproj, *.sln
    for f in files:
        if f.endswith(".csproj"):
            return jsonify({"type": "dotnet", "reason": f"找到 {f}"})
        if f.endswith(".sln"):
            return jsonify({"type": "dotnet", "reason": f"找到 {f}"})

    # PHP: composer.json
    if "composer.json" in files:
        return jsonify({"type": "php", "reason": "找到 composer.json"})

    # Node.js / Vue / React: package.json
    if "package.json" in files:
        # 尝试读取 package.json 判断具体类型
        try:
            with open(os.path.join(path, "package.json"), "r", encoding="utf-8") as f:
                pkg = json.load(f)
            deps = {}
            deps.update(pkg.get("dependencies", {}))
            deps.update(pkg.get("devDependencies", {}))
            scripts = pkg.get("scripts", {})

            if "vue" in deps or "@vue/cli-service" in deps or "vite" in deps and "vue" in str(deps):
                return jsonify({"type": "vue", "reason": "检测到 Vue 项目"})
            if "react" in deps or "react-dom" in deps or "next" in deps:
                return jsonify({"type": "vue", "reason": "检测到 React 项目"})
            if "create-react-app" in deps or "react-scripts" in deps:
                return jsonify({"type": "vue", "reason": "检测到 React 项目"})
            if "nuxt" in deps or "@nuxt/cli" in deps:
                return jsonify({"type": "vue", "reason": "检测到 Nuxt 项目"})

            # 有 build 脚本的 Node.js 后端
            if "build" in scripts:
                return jsonify({"type": "nodejs", "reason": "检测到 Node.js 项目 (有 build 脚本)"})
            return jsonify({"type": "nodejs", "reason": "找到 package.json"})
        except Exception:
            return jsonify({"type": "nodejs", "reason": "找到 package.json"})

    # Static: index.html
    if "index.html" in files:
        return jsonify({"type": "static", "reason": "找到 index.html"})

    return jsonify({"type": "", "reason": "无法识别项目类型"})


@app.route("/api/ssh-test", methods=["POST"])
def test_ssh():
    """测试 SSH 连接"""
    data = request.json
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")

    if not host or not user:
        return jsonify({"success": False, "message": "主机和用户名不能为空"})

    # 构建 SSH 命令
    if password:
        # 使用 sshpass
        cmd = f'sshpass -p "{password}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p {port} {user}@{host} "echo OK"'
    elif key_path:
        cmd = f'ssh -i "{key_path}" -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p {port} {user}@{host} "echo OK"'
    else:
        cmd = f'ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p {port} {user}@{host} "echo OK"'

    rc, stdout, stderr = run_command(cmd, timeout=15)

    if rc == 0 and "OK" in stdout:
        return jsonify({"success": True, "message": "SSH 连接成功！"})
    else:
        err_msg = stderr.strip() or "连接失败"
        return jsonify({"success": False, "message": err_msg})


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

        # 启动部署线程
        deploy_status["running"] = True
        deploy_status["progress"] = 0
        deploy_status["message"] = "准备部署..."
        deploy_status["log"] = []
        deploy_status["success"] = False

        thread = threading.Thread(
            target=run_deploy,
            args=(server_name, module_name, source_path, server_path, data),
        )
        thread.daemon = True
        thread.start()

        return jsonify({"success": True, "message": "部署任务已启动"})


def run_deploy(server_name, module_name, source_path, server_path, data):
    """执行部署流程"""
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")

    def set_progress(pct, msg):
        with deploy_lock:
            deploy_status["progress"] = pct
            deploy_status["message"] = msg

    def run_ssh_cmd(cmd, timeout=120):
        """通过 SSH 执行远程命令"""
        if password:
            prefix = f'sshpass -p "{password}" ssh -o StrictHostKeyChecking=no -p {port} {user}@{host}'
        elif key_path:
            prefix = f'ssh -i "{key_path}" -o StrictHostKeyChecking=no -p {port} {user}@{host}'
        else:
            prefix = f'ssh -o StrictHostKeyChecking=no -p {port} {user}@{host}'

        # 使用 base64 编码避免特殊字符问题
        cmd_b64 = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
        full_cmd = f'{prefix} "echo {cmd_b64} | base64 -d | bash"'
        return run_command(full_cmd, timeout=timeout)

    try:
        # Step 1: 测试 SSH 连接 (10%)
        set_progress(10, "测试 SSH 连接...")
        log_message(f"连接到 {user}@{host}:{port}")
        rc, out, err = run_ssh_cmd("echo SSH_OK", timeout=15)
        if rc != 0:
            set_progress(0, "SSH 连接失败")
            log_message(f"SSH 连接失败: {err}", "error")
            return
        log_message("SSH 连接成功")

        # Step 2: 检查远程 Docker (20%)
        set_progress(20, "检查远程 Docker...")
        rc, out, err = run_ssh_cmd("docker --version")
        if rc != 0:
            set_progress(0, "远程服务器未安装 Docker")
            log_message("远程服务器未安装 Docker", "error")
            return
        log_message(f"远程 Docker: {out.strip()}")

        # Step 3: 检查 Docker Compose (30%)
        set_progress(30, "检查 Docker Compose...")
        rc, out, err = run_ssh_cmd("docker compose version")
        compose_prefix = "docker compose"
        if rc != 0:
            rc2, out2, err2 = run_ssh_cmd("docker-compose --version")
            if rc2 == 0:
                compose_prefix = "docker-compose"
            else:
                log_message("未检测到 Docker Compose，尝试安装...", "warn")
                run_ssh_cmd(
                    "sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose; sudo chmod +x /usr/local/bin/docker-compose",
                    timeout=120,
                )
                compose_prefix = "docker-compose"
        log_message(f"Docker Compose: {compose_prefix}")

        # Step 4: 创建远程目录 (40%)
        set_progress(40, "创建远程目录...")
        remote_dir = f"{server_path}/{module_name}"
        run_ssh_cmd(f"mkdir -p {remote_dir}")
        log_message(f"远程目录: {remote_dir}")

        # Step 5: 同步文件 (50-70%)
        set_progress(50, "同步文件到服务器...")
        log_message("开始同步文件...")

        # 检测 rsync
        has_rsync = subprocess.run("rsync --version", shell=True, capture_output=True).returncode == 0

        if has_rsync:
            log_message("使用 rsync 同步...")
            excludes = [
                ".git",
                "node_modules",
                ".env",
                "*.log",
                ".claude",
                ".vscode",
                "__pycache__",
                ".deploy-logs",
                ".deploy-ssh",
            ]
            exclude_args = " ".join([f"--exclude={e}" for e in excludes])
            ssh_opt = f"ssh -o StrictHostKeyChecking=no -p {port}"
            if key_path:
                ssh_opt += f" -i \"{key_path}\""
            elif password:
                ssh_opt = f"sshpass -p \"{password}\" {ssh_opt}"

            source = source_path.rstrip("\\/").replace("\\", "/") + "/"
            rsync_cmd = f'rsync -avz --delete --no-perms --no-owner --no-group {exclude_args} -e "{ssh_opt}" "{source}" "{user}@{host}:{remote_dir}/"'
            rc, out, err = run_command(rsync_cmd, cwd=None, timeout=300)
            if rc == 0:
                log_message("rsync 同步完成")
            else:
                log_message(f"rsync 失败，切换 scp 模式: {err}", "warn")
                has_rsync = False

        if not has_rsync:
            log_message("使用 scp + tar 同步...")
            # 创建临时压缩包
            temp_zip = os.path.join(tempfile.gettempdir(), f"deploy_{datetime.now().strftime('%Y%m%d%H%M%S')}.tar.gz")
            exclude_args = []
            for e in [".git", "node_modules", ".env", "*.log", ".claude", ".vscode", ".deploy-logs", ".deploy-ssh"]:
                exclude_args.extend(["--exclude", e])

            tar_cmd = ["tar", "cfz", temp_zip] + exclude_args + ["-C", source_path, "."]
            subprocess.run(tar_cmd, capture_output=True)

            if os.path.exists(temp_zip):
                size_mb = round(os.path.getsize(temp_zip) / 1024 / 1024, 2)
                log_message(f"压缩包大小: {size_mb}MB")

                # 上传
                scp_cmd = f'scp -o StrictHostKeyChecking=no -P {port} "{temp_zip}" "{user}@{host}:{remote_dir}/deploy.tar.gz"'
                if key_path:
                    scp_cmd = f'scp -i "{key_path}" -o StrictHostKeyChecking=no -P {port} "{temp_zip}" "{user}@{host}:{remote_dir}/deploy.tar.gz"'
                elif password:
                    scp_cmd = f'sshpass -p "{password}" {scp_cmd}'

                rc, out, err = run_command(scp_cmd, timeout=300)
                if rc == 0:
                    # 远程解压
                    run_ssh_cmd(f"cd {remote_dir}; tar xfz deploy.tar.gz; rm -f deploy.tar.gz")
                    log_message("scp 同步完成")
                else:
                    log_message(f"上传失败: {err}", "error")

                # 清理临时文件
                try:
                    os.remove(temp_zip)
                except Exception:
                    pass

        set_progress(70, "文件同步完成")

        # Step 6: 生成 docker-compose.yml (80%)
        set_progress(80, "生成部署配置...")
        compose_content = data.get("docker_compose", generate_docker_compose(module_name))
        compose_b64 = base64.b64encode(compose_content.encode("utf-8")).decode("ascii")
        run_ssh_cmd(f"echo {compose_b64} | base64 -d > {remote_dir}/docker-compose.yml")
        log_message("docker-compose.yml 已生成")

        # 生成 Dockerfile（如果需要）
        dockerfile_content = data.get("dockerfile", "")
        if dockerfile_content:
            df_b64 = base64.b64encode(dockerfile_content.encode("utf-8")).decode("ascii")
            run_ssh_cmd(f"echo {df_b64} | base64 -d > {remote_dir}/Dockerfile")
            log_message("Dockerfile 已生成")

        # Step 7: 远程 Docker 部署 (90%)
        set_progress(90, "远程构建和部署...")
        log_message("执行 docker compose pull...")
        run_ssh_cmd(f"cd {remote_dir}; {compose_prefix} -f docker-compose.yml pull", timeout=120)

        log_message("执行 docker compose build...")
        rc, out, err = run_ssh_cmd(f"cd {remote_dir}; {compose_prefix} -f docker-compose.yml build", timeout=300)
        if out:
            log_message(out.strip())
        if err:
            log_message(err.strip(), "warn")

        log_message("执行 docker compose up -d...")
        rc, out, err = run_ssh_cmd(f"cd {remote_dir}; {compose_prefix} -f docker-compose.yml up -d --remove-orphans", timeout=120)
        if rc == 0:
            log_message("容器启动成功！")
        else:
            log_message(f"容器启动可能有问题: {err}", "warn")

        # Step 8: 显示状态 (100%)
        set_progress(100, "部署完成！")
        log_message("获取容器状态...")
        rc, out, err = run_ssh_cmd(f"cd {remote_dir}; {compose_prefix} -f docker-compose.yml ps")
        if out:
            log_message(out.strip())

        with deploy_lock:
            deploy_status["success"] = True
    except Exception as e:
        log_message(f"部署出错: {str(e)}", "error")
        with deploy_lock:
            deploy_status["success"] = False
    finally:
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
  http://localhost:5000
""")
    app.run(host="0.0.0.0", port=5000, debug=False)
