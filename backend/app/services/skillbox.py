"""技能脚本的隔离容器。

整只容器跟着接口进程走：启动时构建并拉起，关闭时删除。
容器里预装 Python、Node 和 Java，主进程只是 sleep，不预载任何技能。
技能文件留在宿主机的 backend/data/skills/{编号}/。真正执行时才把这一份
脚本的字节送进容器的 /tmp，跑完删除临时目录。项目目录、数据库和 Docker
套接字都不挂进去。

参数用 exec 的参数数组传递，不拼进 shell，因此脚本参数里的分号或引号
只是 argv 里的普通字符串。
"""

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "limen-skillbox"
CONTAINER = "limen-skillbox"
DOCKERFILE = ROOT / "skillbox" / "Dockerfile"
# 测试进程用临时目录，避免脚本写进正在使用的 backend/data/skills。
SKILL_ROOT = Path(tempfile.mkdtemp(prefix="limen-skills-")) if "pytest" in sys.modules else ROOT / "data" / "skills"
SUFFIXES = {".py": "python3", ".js": "node", ".mjs": "node", ".java": "java"}
_READY = False
_ERROR = ""
_RUNTIME_ID = ""
_RUNTIMES = {"python": "", "node": "", "java": ""}
_JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
# 相加这类脚本往往不到一秒。总览按秒刷新，结束后仍保留一小段，页面才看得到执行了哪一份。
_RECENT_SECONDS = 30


def start() -> None:
    """构建镜像并拉起长期容器。失败只记下原因，不阻止接口启动。

    pytest 已加载时直接返回，避免测试去拉 Docker，也避免脚本落到宿主机解释器。
    本机没有 docker 命令时同样只记错误：技能说明仍可保存，run_script 会拒绝执行。
    """
    global _READY, _ERROR
    if "pytest" in sys.modules:
        return
    if shutil.which("docker") is None:
        _ERROR = "本机没有 docker，技能脚本不能运行"
        return
    try:
        subprocess.run(["docker", "build", "-t", IMAGE, str(DOCKERFILE.parent)], check=True, timeout=600)
        # 上次崩溃可能留下同名容器。先删掉，保证这次的限制参数生效。
        subprocess.run(["docker", "rm", "-f", CONTAINER], check=False, timeout=30, capture_output=True)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER,
                # 允许访问网络，技能脚本可以安装依赖或访问接口。其余隔离限制仍然保留。
                "--network", "bridge",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                # 根文件系统只读。唯一可写位置是下面这块 128MB 内存盘。
                "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,size=128m",
                "--memory", "512m",
                "--cpus", "1",
                "--pids-limit", "64",
                IMAGE,
                "sleep", "infinity",
            ],
            check=True,
            timeout=60,
        )
        _READY = True
        _ERROR = ""
        _remember_runtimes()
    except (OSError, subprocess.SubprocessError) as exc:
        _READY = False
        _ERROR = str(exc)[:300]


def stop() -> None:
    """系统退出时销毁容器，不保留可写层。"""
    global _READY, _RUNTIME_ID
    if "pytest" in sys.modules or shutil.which("docker") is None:
        _READY = False
        return
    subprocess.run(["docker", "rm", "-f", CONTAINER], check=False, timeout=30, capture_output=True)
    _READY = False
    _RUNTIME_ID = ""


def status() -> dict:
    """给总览用的容器快照。不执行技能脚本。"""
    info = {
        "name": CONTAINER,
        "image": IMAGE,
        "state": "unavailable",
        "status": "",
        "started_at": "",
        "memory": "",
        "cpus": "",
        "network": "",
        "read_only": False,
        "python": _RUNTIMES["python"],
        "node": _RUNTIMES["node"],
        "java": _RUNTIMES["java"],
        "error": _ERROR,
    }
    if shutil.which("docker") is None:
        info["error"] = _ERROR or "本机没有 docker，技能脚本不能运行"
        return _with_jobs(info)
    try:
        completed = subprocess.run(
            ["docker", "inspect", CONTAINER],
            check=False,
            timeout=8,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = str(exc)[:300]
        return _with_jobs(info)
    if completed.returncode != 0:
        info["state"] = "missing"
        info["error"] = _ERROR or "技能容器还没有启动"
        return _with_jobs(info)
    import json

    try:
        payload = json.loads(completed.stdout)[0]
    except (json.JSONDecodeError, IndexError, TypeError):
        info["error"] = "无法读取容器状态"
        return _with_jobs(info)
    state = payload.get("State") or {}
    host = payload.get("HostConfig") or {}
    info["state"] = state.get("Status") or "unavailable"
    info["status"] = state.get("Status") or ""
    info["started_at"] = state.get("StartedAt") or ""
    memory = int(host.get("Memory") or 0)
    info["memory"] = f"{memory // (1024 * 1024)} MB" if memory else ""
    nano = int(host.get("NanoCpus") or 0)
    info["cpus"] = f"{nano / 1_000_000_000:g}" if nano else ""
    info["network"] = host.get("NetworkMode") or ""
    info["read_only"] = bool(host.get("ReadonlyRootfs"))
    info["image"] = (payload.get("Config") or {}).get("Image") or IMAGE
    container_id = payload.get("Id") or ""
    if info["state"] == "running" and container_id and container_id != _RUNTIME_ID:
        _remember_runtimes(container_id)
    info["python"] = _RUNTIMES["python"]
    info["node"] = _RUNTIMES["node"]
    info["java"] = _RUNTIMES["java"]
    if info["state"] == "running":
        info["error"] = ""
    return _with_jobs(info)


def _remember_runtimes(container_id: str = "") -> None:
    """版本只在容器换新时查一次，总览刷新不再进容器。"""
    global _RUNTIME_ID
    probes = (
        ("python", ["python3", "--version"]),
        ("node", ["node", "--version"]),
        ("java", ["java", "-version"]),
    )
    for key, args in probes:
        try:
            completed = subprocess.run(
                ["docker", "exec", CONTAINER, *args],
                check=False,
                timeout=8,
                capture_output=True,
                text=True,
            )
            text = (completed.stdout or completed.stderr or "").strip().splitlines()
            _RUNTIMES[key] = text[0][:80] if text else ""
        except (OSError, subprocess.SubprocessError):
            _RUNTIMES[key] = ""
    _RUNTIME_ID = container_id or _RUNTIME_ID


def skill_dir(skill_id: int) -> Path:
    return SKILL_ROOT / str(skill_id)


def storage_path(skill_id: int) -> str:
    """技能文件所在目录。在项目内时给出相对路径，测试临时目录则给出绝对路径。"""
    folder = skill_dir(skill_id)
    try:
        return folder.resolve().relative_to(ROOT.parent.resolve()).as_posix()
    except ValueError:
        return str(folder)


def running_jobs() -> list[dict]:
    """正在执行的脚本，以及刚结束、仍在展示窗口内的脚本。"""
    now = time.time()
    with _JOB_LOCK:
        expired = [
            key for key, item in _JOBS.items()
            if item.get("state") != "running" and now - float(item.get("ended_at") or now) > _RECENT_SECONDS
        ]
        for key in expired:
            _JOBS.pop(key, None)
        rows = [dict(item) for item in _JOBS.values()]
    rows.sort(key=lambda item: (item.get("state") != "running", -float(item.get("started_at") or 0)))
    return rows


def _begin_job(skill_id: int, script: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _JOB_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "skill_id": skill_id,
            "script": script,
            "state": "running",
            "started_at": time.time(),
            "ended_at": None,
        }
    return job_id


def _end_job(job_id: str, state: str = "finished") -> None:
    with _JOB_LOCK:
        item = _JOBS.get(job_id)
        if item is None:
            return
        item["state"] = state if state in {"finished", "failed"} else "finished"
        item["ended_at"] = time.time()


def _with_jobs(info: dict) -> dict:
    jobs = running_jobs()
    info["jobs"] = jobs
    info["script_running"] = any(item.get("state") == "running" for item in jobs)
    return info


def save_script(skill_id: int, filename: str, source: str) -> str:
    """把脚本存成宿主机上的纯文本。此时不进容器。"""
    name = _script_name(filename)
    folder = skill_dir(skill_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text(source or "", encoding="utf-8")
    return name


def read_script(skill_id: int, filename: str) -> str:
    """读回已保存的脚本正文，供编辑界面展示。不进入容器。"""
    name = _script_name(filename)
    target = skill_dir(skill_id) / name
    if not target.is_file():
        raise ValueError("脚本不存在")
    return target.read_text(encoding="utf-8")


def remove_skill_dir(skill_id: int) -> None:
    folder = skill_dir(skill_id)
    if folder.exists():
        shutil.rmtree(folder)


def run_script(skill_id: int, filename: str, args: list | None = None) -> str:
    """在已启动的容器里执行这一份脚本，并返回合并后的输出。

    调用方必须先确认脚本名属于该技能的 scripts_json。这里再挡一次路径和后缀。
    容器未就绪时直接失败，不会改用宿主机的 python、node 或 java。
    """
    if not _READY:
        raise RuntimeError(_ERROR or "技能容器还没有启动")
    name = _script_name(filename)
    source = skill_dir(skill_id) / name
    if not source.is_file():
        raise ValueError("脚本不存在")
    suffix = source.suffix.lower()
    runner = SUFFIXES.get(suffix)
    if runner is None:
        raise ValueError("只接受 Python、JavaScript 或 Java 脚本")
    values = _args(args)
    # 每次调用独占一个临时目录，避免并发执行互相覆盖。
    # 登记从这里开始，总览在脚本拷贝和执行期间都能看到它。
    job = _begin_job(skill_id, name)
    remote = f"/tmp/job-{job}"
    succeeded = False
    try:
        # 65534 是镜像里的 nobody。HOME 指到可写的 /tmp，避免解释器去写只读的 /root。
        _docker(["exec", "-u", "65534:65534", "-e", "HOME=/tmp", CONTAINER, "mkdir", "-p", remote], timeout=10)
        # 根文件系统只读，docker cp 写不进这块 tmpfs。改由容器内的 tee 从标准输入落盘。
        copied = subprocess.run(
            ["docker", "exec", "-i", "-u", "65534:65534", "-e", "HOME=/tmp", CONTAINER, "tee", f"{remote}/{name}"],
            input=source.read_bytes(),
            check=False,
            timeout=15,
            capture_output=True,
        )
        if copied.returncode != 0:
            detail = copied.stderr.decode(errors="replace") if copied.stderr else "无法把脚本送入容器"
            raise RuntimeError(detail[:300])
        if suffix == ".java":
            stem = source.stem
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stem):
                raise ValueError("Java 文件名需要是类名")
            _docker(["exec", "-u", "65534:65534", "-e", "HOME=/tmp", CONTAINER, "javac", "-d", remote, f"{remote}/{name}"], timeout=20)
            # 类名单独作为参数，后面才是脚本自己的 argv。
            command = ["exec", "-u", "65534:65534", "-e", "HOME=/tmp", CONTAINER, "java", "-cp", remote, stem, *values]
        else:
            command = ["exec", "-u", "65534:65534", "-e", "HOME=/tmp", CONTAINER, runner, f"{remote}/{name}", *values]
        completed = _docker(command, timeout=20)
        text = (completed.stdout or "") + (completed.stderr or "")
        succeeded = True
        return text.strip()[:4000] or "脚本没有输出"
    finally:
        _end_job(job, "finished" if succeeded else "failed")
        # 成功、解释器失败或超时都清掉这次的临时目录。宿主机上的技能文件保留。
        subprocess.run(["docker", "exec", CONTAINER, "rm", "-rf", remote], check=False, timeout=10, capture_output=True)


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    completed = subprocess.run(["docker", *args], check=False, timeout=timeout, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "脚本执行失败").strip()
        raise RuntimeError(detail[:500])
    return completed


def _script_name(filename: str) -> str:
    """只接受当前目录下的脚本名，拒绝 ../、绝对路径和隐藏文件。"""
    name = Path(str(filename or "")).name
    if not name or name != filename or name.startswith("."):
        raise ValueError("脚本名不合法")
    if Path(name).suffix.lower() not in SUFFIXES:
        raise ValueError("只接受 Python、JavaScript 或 Java 脚本")
    return name


def _args(args: list | None) -> list[str]:
    """收成短字符串。禁止换行，避免参数被解释成另一条命令或另一段脚本。"""
    values = []
    for item in args or []:
        text = str(item)
        if len(text) > 200 or "\n" in text or "\x00" in text:
            raise ValueError("脚本参数过长")
        values.append(text)
    if len(values) > 8:
        raise ValueError("脚本参数最多 8 个")
    return values
