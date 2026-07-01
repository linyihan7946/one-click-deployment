"""
一键部署 - Web 可视化部署工具
Flask 后端服务
"""
import json
import os
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
from datetime import datetime
from flask import Flask, render_template, request, jsonify

import paramiko

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
            "allow_agent": True,
            "look_for_keys": True,
        }

        # 优先使用密钥
        if self.key_path and os.path.exists(os.path.expanduser(self.key_path)):
            connect_kwargs["key_filename"] = os.path.expanduser(self.key_path)
        elif self.password:
            connect_kwargs["password"] = self.password

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
            def _should_exclude(name):
                name_lower = name.lower()
                for exc in excludes:
                    exc = exc.strip("/").lower()
                    if "*" in exc or "?" in exc:
                        if fnmatch.fnmatch(name_lower, exc):
                            return True
                    else:
                        if exc == name_lower or exc in name_lower:
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

            def _upload_recursive(local, remote):
                self._ensure_remote_dir(sftp, remote)

                try:
                    items = os.listdir(local)
                except PermissionError:
                    return
                except OSError:
                    return

                for item in items:
                    if _should_exclude(item):
                        continue

                    local_path = os.path.join(local, item)
                    remote_path = remote + "/" + item

                    if os.path.islink(local_path):
                        continue

                    if os.path.isdir(local_path):
                        _upload_recursive(local_path, remote_path)
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
    return render_template("index.html")


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

    return jsonify({"type": "", "reason": "无法识别项目类型"})


@app.route("/api/ssh-test", methods=["POST"])
def test_ssh():
    """测试 SSH 连接（使用 paramiko）"""
    data = request.json
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")

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
    """执行部署流程（使用 paramiko）"""
    host = data.get("host", "")
    port = data.get("port", 22)
    user = data.get("user", "")
    password = data.get("password", "")
    key_path = data.get("key_path", "")

    ssh = None

    def set_progress(pct, msg):
        with deploy_lock:
            deploy_status["progress"] = pct
            deploy_status["message"] = msg

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
        ok, create_mode, create_error = ssh.prepare_remote_dir(remote_dir)
        if not ok:
            log_message(f"创建远程目录失败: {create_error}", "error")
            set_progress(0, "创建远程目录失败")
            return
        if create_mode != "plain":
            log_message("远程目录需要 sudo 初始化，已授权给当前 SSH 用户", "warn")
        log_message(f"远程目录: {remote_dir}")

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
        compose_content = data.get("docker_compose", "")
        if compose_content:
            # 通过 SFTP 写入文件
            sftp = ssh.client.open_sftp()
            with sftp.file(f"{remote_dir}/docker-compose.yml", "w") as f:
                f.write(compose_content)
            log_message("docker-compose.yml 已写入")

        # 写入 Dockerfile
        dockerfile_content = data.get("dockerfile", "")
        if dockerfile_content:
            sftp = ssh.client.open_sftp()
            with sftp.file(f"{remote_dir}/Dockerfile", "w") as f:
                f.write(dockerfile_content)
            log_message("Dockerfile 已写入")

        # Step 7: 远程 Docker 部署 (90%)
        set_progress(90, "远程构建和部署...")
        log_message("执行 docker compose pull...")
        ssh.exec_command(f"cd -- {remote_dir_q} && {compose_prefix} -f docker-compose.yml pull", timeout=120)

        log_message("执行 docker compose build...")
        rc, out, err = ssh.exec_command(f"cd -- {remote_dir_q} && {compose_prefix} -f docker-compose.yml build", timeout=600)
        if out:
            for line in out.strip().split("\n"):
                if line.strip():
                    log_message(line.strip())
        if err:
            for line in err.strip().split("\n"):
                if line.strip():
                    log_message(line.strip(), "warn")
        if rc != 0:
            set_progress(0, "Docker 构建失败")
            log_message("Docker 构建失败，已停止部署", "error")
            return

        log_message("执行 docker compose up -d...")
        rc, out, err = ssh.exec_command(
            f"cd -- {remote_dir_q} && {compose_prefix} -f docker-compose.yml up -d --remove-orphans", timeout=120
        )
        if rc == 0:
            log_message("容器启动成功！")
        else:
            set_progress(0, "容器启动失败")
            log_message(f"容器启动失败: {err or out}", "error")
            return

        # Step 8: 显示状态 (100%)
        set_progress(100, "部署完成！")
        log_message("获取容器状态...")
        rc, out, err = ssh.exec_command(f"cd -- {remote_dir_q} && {compose_prefix} -f docker-compose.yml ps")
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
  http://localhost:5000
""")
    app.run(host="0.0.0.0", port=5000, debug=False)
