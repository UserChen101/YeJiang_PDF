"""检查更新：向 GitHub 读取最新 Release，与当前版本比较。

设计约束
--------
* **只在用户点击「检查更新」时联网**，启动时不自动请求 —— 页匠对外承诺
  「文件不出本机」，任何网络行为都必须是显式触发的，且只读取版本元数据，
  不上传任何本机信息（GitHub 那边只会看到一次 HTTPS 请求）。
* 主通道走 GitHub REST API；API 被限流或不可达时，退到
  `github.com/.../releases/latest` 的重定向地址解析出版本号，
  至少还能告诉用户「有新版本」，只是拿不到更新说明。
* 版本比较按数字段逐段比（1.0.10 > 1.0.9），忽略前缀 v 与预发布后缀。
* 所有网络异常都翻译成中文短句，直接展示给用户。
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any

REPO = "UserChen101/YeJiang_PDF"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
API_LIST = f"https://api.github.com/repos/{REPO}/releases?per_page=1"
RELEASES_LATEST_PAGE = f"https://github.com/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
TIMEOUT = 8           # 秒；太短在国内网络下容易误判为「失败」
NOTES_LIMIT = 2000    # 更新说明最多展示这么多字符


class UpdateError(Exception):
    """检查更新失败（网络、限流、返回格式异常）。message 已是中文。"""


# ---------------------------------------------------------------- 版本比较

def parse_version(text: str) -> tuple[int, ...]:
    """'v1.0.10' / '1.0.4' / '1.0.4-beta.1' → (1, 0, 10)。解析不出数字返回 ()。"""
    if not text:
        return ()
    m = re.search(r"(\d+(?:\.\d+)*)", str(text))
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新。缺段按 0 补齐（1.1 == 1.1.0）。"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


# ---------------------------------------------------------------- 网络

def _request(url: str) -> bytes:
    from .. import __version__  # 延迟导入，避免包初始化顺序问题

    req = urllib.request.Request(
        url,
        headers={
            # GitHub API 强制要求 UA，缺了会直接 403
            "User-Agent": f"PageSmith/{__version__} (+https://github.com/{REPO})",
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
        },
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return resp.read()


def _translate(exc: Exception) -> UpdateError:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return UpdateError("仓库里还没有发布任何版本")
        if exc.code in (403, 429):
            return UpdateError("GitHub 接口访问太频繁被暂时限流，请过几分钟再试")
        return UpdateError(f"GitHub 返回错误（HTTP {exc.code}）")
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return UpdateError("连接 GitHub 超时，请检查网络后重试")
        if isinstance(reason, ssl.SSLError):
            return UpdateError("与 GitHub 建立加密连接失败，可能是网络代理拦截")
        return UpdateError("无法连接到 GitHub，请检查网络后重试")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return UpdateError("连接 GitHub 超时，请检查网络后重试")
    return UpdateError(f"检查更新失败：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 解析

def _clean_notes(body: str) -> str:
    """把 Release 的 Markdown 正文压成适合弹窗展示的纯文本。"""
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    text = re.sub(r"```.*?```", "", text, flags=re.S)          # 代码块
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)            # 图片
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # 链接留文字
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)   # 标题符号
    text = re.sub(r"[*_`>]|^\s*[-+]\s+", lambda m: "· " if m.group(0).strip() in "-+" else "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > NOTES_LIMIT:
        text = text[:NOTES_LIMIT].rstrip() + "…"
    return text.strip()


def _pick_installer(assets: list[dict[str, Any]]) -> dict[str, str] | None:
    """优先挑安装包（PageSmithSetup-*.exe），其次任意 exe，再其次 zip。"""
    def score(name: str) -> int:
        low = name.lower()
        if low.endswith(".exe") and "setup" in low:
            return 0
        if low.endswith(".exe"):
            return 1
        if low.endswith(".zip"):
            return 2
        return 9

    candidates = [
        (score(a.get("name", "")), a)
        for a in assets
        if a.get("browser_download_url") and score(a.get("name", "")) < 9
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1].get("name", "")))
    best = candidates[0][1]
    return {
        "name": best.get("name", ""),
        "url": best["browser_download_url"],
        "size": best.get("size") or 0,
        "downloads": best.get("download_count") or 0,
    }


def _from_api() -> dict[str, Any]:
    raw = _request(API_LATEST)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("GitHub 返回的内容无法解析") from exc
    tag = data.get("tag_name") or data.get("name") or ""
    version = ".".join(str(x) for x in parse_version(tag)) or tag
    return {
        "version": version,
        "tag": tag,
        "name": data.get("name") or tag,
        "notes": _clean_notes(data.get("body") or ""),
        "url": data.get("html_url") or RELEASES_PAGE,
        "published_at": data.get("published_at") or "",
        "prerelease": bool(data.get("prerelease")),
        "installer": _pick_installer(data.get("assets") or []),
        "source": "api",
    }


def _from_redirect() -> dict[str, Any]:
    """API 不可用时的兜底：解析 /releases/latest 重定向到哪个 tag。"""
    from .. import __version__

    req = urllib.request.Request(
        RELEASES_LATEST_PAGE,
        headers={"User-Agent": f"PageSmith/{__version__}", "Accept-Encoding": "identity"},
    )
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        final = resp.geturl()
    m = re.search(r"/releases/tag/([^/?#]+)", final)
    if not m:
        raise UpdateError("无法从 GitHub 解析最新版本号")
    tag = m.group(1)
    version = ".".join(str(x) for x in parse_version(tag)) or tag
    return {
        "version": version,
        "tag": tag,
        "name": tag,
        "notes": "",
        "url": final,
        "published_at": "",
        "prerelease": False,
        "installer": None,
        "source": "redirect",
    }


# ---------------------------------------------------------------- 对外

def check(current: str = "") -> dict[str, Any]:
    """查询最新版本。成功返回结构化结果，失败抛 UpdateError。"""
    from .. import __version__

    cur = current or __version__
    errors: list[str] = []
    latest: dict[str, Any] | None = None
    for fetcher in (_from_api, _from_redirect):
        try:
            latest = fetcher()
            break
        except UpdateError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001 — 兜底翻译，不让堆栈上界面
            errors.append(str(_translate(exc)))

    if latest is None:
        raise UpdateError(errors[0] if errors else "检查更新失败")

    latest["current"] = cur
    latest["has_update"] = is_newer(latest["version"], cur)
    latest["releases_url"] = RELEASES_PAGE
    latest["repo"] = REPO
    # 兜底通道拿不到安装包直链时，给用户可点的发布页
    if not latest.get("installer"):
        latest["installer"] = None
    return latest
