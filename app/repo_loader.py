"""仓库抓取 / 过滤 / 排序 / 截断。

产出给解剖员的一段纯文本（目录树 + 代码内容），以及透明度计数。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx

from . import config

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    ".idea",
    ".vscode",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "vendor",
}

SKIP_FILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
}

SKIP_EXTS = {
    # 二进制 / 媒体 / 字体 / 压缩包 / 可执行
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".svgz",
    ".mp3", ".mp4", ".wav", ".mov", ".avi", ".mkv", ".webm", ".flac", ".ogg",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".jar", ".war", ".apk", ".ipa",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".lib", ".class", ".pyc",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".psd", ".ai", ".sketch", ".fig", ".blend",
    ".sqlite", ".db", ".lock",
}

CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs",
    ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".m", ".scala", ".sql", ".sh", ".ps1", ".dart", ".lua", ".r", ".jl",
    ".html", ".css", ".scss", ".sass", ".less", ".md", ".txt", ".yaml", ".yml",
    ".toml", ".json", ".xml", ".ini", ".cfg", ".env.example",
}

ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "manage.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "index.jsx", "index.tsx", "server.js", "server.ts",
    "main.go", "main.rs", "main.c", "main.cpp", "main.java", "app.js", "app.ts",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile",
}

DEP_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile",
    "package.json", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts", "composer.json", "gemfile", "pubspec.yaml", "cmakelists.txt",
}

MAX_SINGLE_FILE_BYTES = 2_000_000


def _github_parts(repo_url: str) -> tuple[str, str]:
    url = repo_url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}"
    parsed = urlparse(url)
    if parsed.netloc not in {"github.com", "www.github.com"}:
        raise ValueError("目前只支持 GitHub 仓库地址（https://github.com/owner/repo）")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("仓库地址格式应为 https://github.com/owner/repo")
    return parts[0], parts[1]


def _clone(repo_url: str, dest: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", repo_url, str(dest)],
            capture_output=True,
            timeout=180,
        )
        return proc.returncode == 0 and dest.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def _download_zip(repo_url: str, dest: Path) -> None:
    owner, repo = _github_parts(repo_url)
    archive_url = f"https://github.com/{owner}/{repo}/archive/HEAD.zip"
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        with client.stream("GET", archive_url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                downloaded = 0
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > 200 * 1024 * 1024:
                        raise ValueError("仓库压缩包超过 200MB，请换一个小一点的仓库")


def _extract_zip(zip_path: Path, extract_to: Path) -> Path:
    with zipfile.ZipFile(zip_path) as zf:
        base = extract_to.resolve()
        for member in zf.infolist():
            target = (extract_to / member.filename).resolve()
            if str(target) == str(base) or str(target).startswith(str(base) + os.sep):
                continue
            raise ValueError("压缩包包含非法路径，已中止")
        zf.extractall(extract_to)

    children = [p for p in extract_to.iterdir() if p.is_dir()]
    if len(children) == 1:
        return children[0]
    return extract_to


def _fetch_repo(repo_url: str) -> tuple[Path, str]:
    """返回 (仓库根目录, 临时工作目录)。临时目录由调用方清理。"""
    _github_parts(repo_url)  # 提前校验，不合法直接报错
    work = Path(tempfile.mkdtemp(prefix="pitchmaster_repo_"))
    repo_dir = work / "repo"

    if _clone(repo_url, repo_dir):
        return repo_dir, str(work)

    try:
        shutil.rmtree(repo_dir, ignore_errors=True)
        zip_path = work / "archive.zip"
        extract_dir = work / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        _download_zip(repo_url, zip_path)
        root = _extract_zip(zip_path, extract_dir)
        return root, str(work)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            sample = fh.read(4096)
    except OSError:
        return True
    return b"\x00" in sample


def _score(rel: str) -> tuple[int, int]:
    name = PurePosixPath(rel).name.lower()
    depth = rel.count("/")
    suffix = PurePosixPath(rel).suffix.lower()

    if name.startswith("readme"):
        return (0, depth)
    if name in DEP_NAMES:
        return (1, depth)
    if name in ENTRY_NAMES:
        return (2, depth)
    if rel.lower().startswith("src/") or "/src/" in rel.lower():
        return (3, depth)
    if suffix in CODE_EXTS:
        return (4, depth)
    return (5, depth)


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _walk(root: Path) -> tuple[list[tuple[str, Path, int]], int]:
    candidates: list[tuple[str, Path, int]] = []
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and d != ".git"
        )
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            rel = path.relative_to(root).as_posix()

            if filename.lower() in SKIP_FILE_NAMES:
                skipped += 1
                continue
            if path.suffix.lower() in SKIP_EXTS:
                skipped += 1
                continue
            try:
                size = path.stat().st_size
            except OSError:
                skipped += 1
                continue
            if size == 0 or size > MAX_SINGLE_FILE_BYTES:
                skipped += 1
                continue
            if _is_binary(path):
                skipped += 1
                continue

            candidates.append((rel, path, size))

    candidates.sort(key=lambda item: _score(item[0]))
    return candidates, skipped


def _collect(root: Path, repo_url: str) -> dict:
    candidates, skipped = _walk(root)

    tree_lines = [rel for rel, _, _ in candidates[:300]]
    if len(candidates) > 300:
        tree_lines.append(f"...（目录树仅显示前 300 个文件，共 {len(candidates)} 个）")

    parts = [
        f"# 仓库：{repo_url}",
        "# 目录树",
        "\n".join(tree_lines),
        "",
        "# 文件内容",
    ]
    total = sum(len(p) + 1 for p in parts)
    files_read = 0

    for rel, path, _ in candidates:
        if total >= config.MAX_TOTAL_CHARS:
            break
        try:
            content = _decode(path.read_bytes())
        except OSError:
            skipped += 1
            continue

        if len(content) > config.MAX_FILE_CHARS:
            content = content[: config.MAX_FILE_CHARS] + "\n...[单文件截断]"

        block = f"\n### FILE: {rel}\n```\n{content}\n```\n"
        remaining = config.MAX_TOTAL_CHARS - total
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[: max(0, remaining - 16)] + "\n...[总量截断]\n"

        parts.append(block)
        total += len(block)
        files_read += 1

    text = "\n".join(parts)
    # join 会产生额外分隔符，最后再兜底裁一次，确保严格 ≤ 300k
    if len(text) > config.MAX_TOTAL_CHARS:
        text = text[: config.MAX_TOTAL_CHARS - 20] + "\n...[总量截断]\n"

    files_skipped = skipped + max(0, len(candidates) - files_read)
    return {
        "text": text,
        "files_read": files_read,
        "files_skipped": files_skipped,
        "files_total": len(candidates),
    }


def load_repository(repo_url: str) -> dict:
    """同步入口（调用方用 asyncio.to_thread 包一层）。"""
    root, work = _fetch_repo(repo_url)
    try:
        result = _collect(root, repo_url)
        result["repo_url"] = repo_url
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)
