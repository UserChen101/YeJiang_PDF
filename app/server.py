"""本地资源服务：给 WebView 提供静态页面 + PDF 字节流 + 缩略图。

为什么要自己起 HTTP 而不是用 file://
------------------------------------
* file:// 下 fetch 本地文件会被 CORS / 安全策略拦掉，pdf.js 拿不到 ArrayBuffer。
* 自己起服务可以顺手实现 Range 请求，几千页的大文件也能边下边看。
* 只监听 127.0.0.1 且随机端口 + 一次性 token，外部无法访问。
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pymupdf as fitz  # PyMuPDF

from .core.doc import DocumentSession

mimetypes.add_type("application/pdf", ".pdf")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("font/woff2", ".woff2")


class _Handler(BaseHTTPRequestHandler):
    server_version = "PageSmith/1.0"
    protocol_version = "HTTP/1.1"

    # 由 start_server 注入
    web_root: Path = Path(".")
    session: DocumentSession | None = None
    quiet: bool = True
    # 缩略图缓存：(token, page, width) -> jpeg bytes
    thumb_cache: dict[tuple, bytes] = {}
    cache_root: Path = Path(".")

    # ---------------------------------------------------------------- 日志

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if not self.quiet:
            print(f"[http] {self.address_string()} {fmt % args}")

    def handle_one_request(self) -> None:
        """WebView2 会做无请求的 TCP 预连接，重置连接是正常现象，静默处理。"""
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    # ---------------------------------------------------------------- 路由

    def do_HEAD(self) -> None:  # noqa: N802
        self._route(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._route(head_only=False)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload(parse_qs(parsed.query))
        elif parsed.path == "/api/log":
            self._handle_log()
        else:
            self._json({"ok": False, "error": "not found"}, status=404)

    def _handle_log(self) -> None:
        """接收前端运行时错误，打印到宿主终端，便于排查。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            data = json.loads(raw or "{}")
            print(f"[frontend:{data.get('level', 'error')}] {data.get('msg', '')}")
            if data.get("src"):
                print(f"    at {data['src']}:{data.get('line', '?')}")
            if data.get("stack"):
                for line in str(data["stack"]).splitlines()[:6]:
                    print(f"    {line.strip()}")
            sys.stdout.flush()
        except Exception as exc:
            print(f"[frontend] 日志解析失败: {exc}")
        self._json({"ok": True})

    def _handle_upload(self, query: dict) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 512 * 1024 * 1024:
                self._json({"ok": False, "error": "文件为空或超过 512MB"}, status=400)
                return
            data = self.rfile.read(length)
            raw_name = (query.get("name") or ["upload.pdf"])[0]
            name = Path(raw_name).name or "upload.pdf"
            if not name.lower().endswith(".pdf"):
                name += ".pdf"
            target = self.cache_root / f"drop_{int(time.time() * 1000)}_{name}"
            target.write_bytes(data)
            self._json({"ok": True, "path": str(target)})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=500)

    def _route(self, head_only: bool) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        try:
            if path in ("/doc.pdf", "/api/doc.pdf"):
                self._serve_doc(query, head_only)
            elif path == "/printview":
                self._serve_printview(query, head_only)
            elif path.startswith("/api/thumb/"):
                page = int(path.rsplit("/", 1)[-1].split(".")[0])
                self._serve_thumb(page, query, head_only)
            elif path.startswith("/api/print/"):
                page = int(path.rsplit("/", 1)[-1].split(".")[0])
                self._serve_print(page, query, head_only)
            elif path == "/api/ping":
                self._json({"ok": True})
            else:
                self._serve_static(path, head_only)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self._json({"ok": False, "error": str(exc)}, status=500)

    # ---------------------------------------------------------------- 具体处理

    def _serve_doc(self, query: dict, head_only: bool) -> None:
        sess = self.session
        if sess is None or sess.doc is None:
            self._json({"ok": False, "error": "no document"}, status=404)
            return
        want = (query.get("t") or [""])[0]
        if want and want != sess.token:
            # token 过期说明文档已被修改，让前端重新取 meta
            self._json({"ok": False, "error": "stale token", "token": sess.token}, status=409)
            return
        data = sess.to_bytes()
        self._send_bytes(data, "application/pdf", head_only, cache="no-store")

    def _serve_thumb(self, page_index: int, query: dict, head_only: bool) -> None:
        sess = self.session
        if sess is None or sess.doc is None or not (0 <= page_index < sess.doc.page_count):
            self._json({"ok": False, "error": "bad page"}, status=404)
            return
        width = int(float((query.get("w") or ["140"])[0]))
        key = (sess.token, page_index, width)
        data = self.thumb_cache.get(key)
        if data is None:
            try:
                p = sess.doc[page_index]
                rect = p.rect
                zoom = max(0.02, width / max(rect.width, 1))
                pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                data = pix.tobytes("jpeg", jpg_quality=72)
            except Exception:
                data = b""
            if data:
                # 限制缓存规模，避免长时间浏览把内存吃满
                if len(self.thumb_cache) > 900:
                    self.thumb_cache.clear()
                self.thumb_cache[key] = data
        if not data:
            self._json({"ok": False, "error": "render failed"}, status=500)
            return
        self._send_bytes(data, "image/jpeg", head_only, cache="public, max-age=120")

    def _serve_print(self, page_index: int, query: dict, head_only: bool) -> None:
        """打印专用高清整页渲染（150dpi 默认，可 72–300）。

        屏幕缩略图 72 质量的 JPEG 打出来会发虚，这里单独走一档：
        按物理 DPI 放大渲染 + 较高质量 JPEG。缓存独立于缩略图，
        规模也小得多（打印一次通常几十页）。
        """
        sess = self.session
        if sess is None or sess.doc is None or not (0 <= page_index < sess.doc.page_count):
            self._json({"ok": False, "error": "bad page"}, status=404)
            return
        try:
            dpi = int(float((query.get("dpi") or ["150"])[0]))
        except ValueError:
            dpi = 150
        dpi = max(72, min(dpi, 300))
        key = ("print", sess.token, page_index, dpi)
        data = self.thumb_cache.get(key)
        if data is None:
            try:
                p = sess.doc[page_index]
                zoom = dpi / 72.0
                pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                data = pix.tobytes("jpeg", jpg_quality=85)
            except Exception:
                data = b""
            if data:
                if len(self.thumb_cache) > 900:
                    self.thumb_cache.clear()
                self.thumb_cache[key] = data
        if not data:
            self._json({"ok": False, "error": "render failed"}, status=500)
            return
        self._send_bytes(data, "image/jpeg", head_only, cache="no-store")

    _PRINTVIEW_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>打印预览 - 页匠</title>
<style>
  @page { margin: 0; size: __ORIENT__; }
  html, body { margin: 0; padding: 0; }
  img { display: block; width: 100%; }
  #bar { position: fixed; top: 0; left: 0; right: 0; height: 52px;
         background: #fff; border-bottom: 1px solid #d0d3d8;
         display: flex; align-items: center; gap: 10px; padding: 0 14px;
         font: 14px/1 "Segoe UI", "Microsoft YaHei", sans-serif; z-index: 9; }
  #bar button { height: 32px; padding: 0 16px; font-size: 14px; cursor: pointer;
                border: 1px solid #c9ccd1; background: #fff; border-radius: 4px; }
  #bar button.primary { background: #1a73e8; border-color: #1a73e8; color: #fff; }
  #status { color: #66707a; }
  @media screen {
    body { background: #525659; padding: 66px 0 30px; }
    .sheet { margin: 0 auto 18px; background: #fff;
             box-shadow: 0 1px 6px rgba(0, 0, 0, .4); }
    .sheet.portrait { width: 780px; }
    .sheet.landscape { width: 1060px; }
  }
  @media print {
    #bar { display: none !important; }
    body { padding: 0; background: #fff; }
    .sheet { page-break-after: always; break-after: page; overflow: hidden; }
    .sheet:last-child { page-break-after: auto; break-after: auto; }
    /* 关键：图框精确等于一页纸（100vw x 100vh）+ object-fit:contain，
       任何纸张方向都不会溢出。此前 img 用 width:100%，横向页在横向纸
       上因亚像素舍入高出约 1px，每页后面都会跟出一张空白页。 */
    .sheet img { width: 100vw; height: 100vh; object-fit: contain; }
  }
</style>
</head>
<body>
<div id="bar">
  <button class="primary" onclick="window.print()">打 印</button>
  <button onclick="closeWin()">关闭窗口</button>
  <span id="status">__PAGES__ 页，正在打开打印对话框…</span>
</div>
__IMGS__
<script>
  function bridge() { return window.pywebview && window.pywebview.api; }
  function closeWin() {
    if (bridge()) { try { bridge().print_done(); return; } catch (e) {} }
    window.close();
  }
  async function waitBridge() {
    for (let i = 0; i < 50; i++) { if (bridge()) return true;
      await new Promise((r) => setTimeout(r, 100)); }
    return false;
  }
  window.addEventListener('load', async () => {
    await waitBridge();
    setTimeout(async () => {
      try { window.print(); } catch (e) {}
      // print() 返回即对话框已关闭（打印或取消），关闭本窗口。
      // 注意绝不能在主窗口调用 print()：WebView2 关闭打印对话框后
      // 可能停止刷新主窗口画面（已知渲染冻结问题），所以打印必须
      // 在这个独立的预览窗口里进行，用完即毁。
      setTimeout(closeWin, 150);
    }, 300);
  });
</script>
</body>
</html>"""

    def _serve_printview(self, query: dict, head_only: bool) -> None:
        sess = self.session
        if sess is None or sess.doc is None:
            self._json({"ok": False, "error": "no document"}, status=404)
            return
        want = (query.get("t") or [""])[0]
        if not want or want != sess.token:
            self._json({"ok": False, "error": "stale token"}, status=409)
            return
        try:
            dpi = int(float((query.get("dpi") or ["150"])[0]))
        except ValueError:
            dpi = 150
        dpi = max(72, min(dpi, 300))
        pages = sess.doc.page_count
        landscape_votes = 0
        sheets = []
        for i in range(pages):
            try:
                rect = sess.doc[i].rect
                w, h = rect.width, rect.height
            except Exception:  # noqa: BLE001
                w, h = 595.0, 842.0
            if w > h:
                landscape_votes += 1
            cls = "landscape" if w > h else "portrait"
            sheets.append(
                f'<div class="sheet {cls}">'
                f'<img src="/api/print/{i}?dpi={dpi}&t={want}" alt="第 {i + 1} 页"></div>'
            )
        imgs = "".join(sheets)
        # 按文档整体方向预设打印纸张方向，用户无需在对话框里手动改横向
        orient = "landscape" if landscape_votes * 2 > pages else "portrait"
        html = (
            self._PRINTVIEW_TMPL
            .replace("__IMGS__", imgs)
            .replace("__PAGES__", str(pages))
            .replace("__ORIENT__", orient)
        )
        self._send_bytes(
            html.encode("utf-8"), "text/html; charset=utf-8", head_only, cache="no-store"
        )

    def _serve_static(self, path: str, head_only: bool) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (self.web_root / rel).resolve()
        root = self.web_root.resolve()
        # 目录穿越防护
        if root not in target.parents and target != root:
            self._json({"ok": False, "error": "forbidden"}, status=403)
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.exists() or not target.is_file():
            self._json({"ok": False, "error": f"not found: {rel}"}, status=404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self._send_bytes(data, ctype, head_only)

    # ---------------------------------------------------------------- 输出

    def _send_bytes(self, data: bytes, ctype: str, head_only: bool, cache: str = "no-cache") -> None:
        total = len(data)
        start, end = 0, total - 1
        partial = False

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0]
            a, _, b = spec.partition("-")
            if a.strip():
                start = int(a)
                end = int(b) if b.strip() else total - 1
            elif b.strip():
                start = max(0, total - int(b))
            end = min(end, total - 1)
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        chunk = data[start : end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if not head_only:
            self.wfile.write(chunk)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class LocalServer:
    def __init__(self, web_root: Path, session: DocumentSession) -> None:
        self.web_root = Path(web_root)
        self.session = session
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> int:
        """绑定配置并启动服务。

        注意：不要用 type() 动态创建 handler 子类 —— 实测 WebView2 的
        网络服务进程无法从动态类拿不到正确响应（页面永远加载不出），
        必须直接给 _Handler 的类属性赋值。
        """
        _Handler.web_root = self.web_root
        _Handler.session = self.session
        _Handler.quiet = os.environ.get("PDFSTUDIO_VERBOSE") == "1"
        _Handler.thumb_cache = {}
        _Handler.cache_root = self.session._tmpdir

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="pagesmith-http")
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"
