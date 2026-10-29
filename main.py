"""页匠 PageSmith —— 桌面端 PDF 工作站（入口）。

启动顺序
--------
1. 创建文档会话（单文档模型，够用且省内存）
2. 起本地 HTTP 服务（静态资源 + 文档字节流 + 缩略图）
3. 打开 WebView 窗口，注入 js_api

已知坑（务必保留这些结论，避免回归）
------------------------------------
* 不要用 type() 动态创建 HTTPHandler 子类 —— WebView2 网络进程无法从
  动态类拿到响应，页面会永远空白。
* js_api 对象的公开属性会被 pywebview 递归序列化，持有 fitz.Document
  这类深对象会撑爆递归，内部引用一律加下划线前缀。
* private_mode=False 时 WebView2 的用户数据目录一旦损坏（异常退出），
  之后所有窗口都会白屏。因此我们把 storage_path 固定到应用私有目录，
  坏了删掉即可重建，不污染 %APPDATA%。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

# 打包后 sys.path 可能不含源码根目录
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _redirect_when_frozen() -> None:
    """打包成无控制台窗口的 exe 后，stdout/stderr 是空的 —— 启动一旦失败
    就只剩"双击没反应"。这里把输出重定向到应用私有日志，出问题有据可查。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        log_dir = Path.home() / ".pagesmith" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / "startup.log", "a", encoding="utf-8", buffering=1)
        fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        sys.stdout = fh
        sys.stderr = fh
    except Exception:
        pass


_redirect_when_frozen()

import logging  # noqa: E402

if os.environ.get("PDFSTUDIO_DEBUG"):
    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s %(name)s] %(message)s")

import webview  # noqa: E402

APP_NAME = "页匠"            # 界面/对话框显示名
APP_NAME_EN = "PageSmith"    # 文件名、互斥量、标识用（纯 ASCII）
VERSION = "1.0.2"


def _fatal_startup(detail: str, hint: str) -> None:
    """启动期的致命错误：日志 + 中文对话框。

    打包成窗口程序后既没有控制台，Python 的 traceback 也只会躺在日志里，
    用户看到的就是"双击没反应"或者一串看不懂的英文。这里至少把
    人话讲清楚，并给出可执行的解决办法。
    """
    text = f"{detail}\n\n{hint}"
    try:
        print(text, flush=True)          # 已重定向到 startup.log
    except Exception:
        pass
    if sys.platform.startswith("win") and not os.environ.get("PDFSTUDIO_NO_DIALOG"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, f"{APP_NAME} 启动失败", 0x10)
        except Exception:
            pass


def describe_import_failure(exc: ImportError) -> tuple[str, str]:
    """把导入失败翻译成（标题正文, 解决建议）。纯函数，方便单测。"""
    msg = str(exc)
    log = Path.home() / ".pagesmith" / "logs" / "startup.log"
    if "应用程序控制策略" in msg or "blocked by application control" in msg.lower():
        return (
            f"无法加载 PDF 核心组件（PyMuPDF）。\n\n系统返回：{msg}",
            "这是 Windows 的「智能应用控制」或公司电脑的代码完整性策略"
            "拦截了未签名的程序文件，不是软件损坏。\n\n"
            "可以按下面任一种方式解决：\n"
            "1. 卸载后重新安装一次 —— 本机实测这样即可恢复；\n"
            "2. 改用便携版：解压 PageSmith-win64.zip，直接运行 PageSmith.exe；\n"
            "3. 关闭「智能应用控制」：Windows 安全中心 → 应用和浏览器控制 → "
            "智能应用控制 → 关闭（注意：关闭后需重装系统才能重新开启）；\n"
            "4. 公司电脑请联系 IT，把安装目录加入允许列表。\n\n"
            f"详细日志：{log}",
        )
    return (
        f"启动失败：无法导入程序模块。\n\n{exc}",
        f"详细日志：{log}",
    )


def _import_backend() -> None:
    """导入后端，并把"被系统安全策略拦截"翻译成人话。

    Windows 11 的「智能应用控制」(Smart App Control) 依据 WDAC 策略
    (Policy ID {0283ac0f-...}) 拦截未签名的原生 DLL，PyMuPDF 的
    ``_mupdf.pyd`` 首当其冲。表现是 ImportError: DLL load failed ...
    应用程序控制策略已阻止此文件 —— 文件本身没问题，是系统不让加载。
    """
    global Api, DocumentSession, LocalServer
    try:
        from app.api import Api as _Api
        from app.core.doc import DocumentSession as _Session
        from app.server import LocalServer as _Server
    except ImportError as exc:
        _fatal_startup(*describe_import_failure(exc))
        raise SystemExit(1)
    Api, DocumentSession, LocalServer = _Api, _Session, _Server


_import_backend()  # noqa: E402


def bundle_root() -> Path:
    """PyInstaller 单文件模式下资源解压在 _MEIPASS。"""
    return Path(getattr(sys, "_MEIPASS", ROOT))


def find_web_root() -> Path:
    base = bundle_root()
    for candidate in (base / "app" / "web", base / "web", ROOT / "app" / "web"):
        if (candidate / "index.html").exists():
            return candidate
    raise FileNotFoundError("找不到界面资源目录 app/web")


def prepare_storage() -> str:
    """WebView2 的私有数据目录。体积异常时自动重建。"""
    storage = Path.home() / ".pagesmith" / "webview"
    try:
        if storage.exists():
            total = sum(f.stat().st_size for f in storage.rglob("*") if f.is_file())
            if total > 200 * 1024 * 1024:
                shutil.rmtree(storage, ignore_errors=True)
    except Exception:
        pass
    storage.mkdir(parents=True, exist_ok=True)
    return str(storage)


_MUTEX_HANDLE = None

_OPEN_EVENT_NAME = r"Local\PageSmith.OpenEvent.v1"
_PENDING_OPEN = Path.home() / ".pagesmith" / "pending_open.json"


def signal_existing_instance(pdf_path: str) -> None:
    """已有实例在跑时：把文件路径落盘 + 触发命名事件，让旧窗口去打开。"""
    try:
        import json

        _PENDING_OPEN.parent.mkdir(parents=True, exist_ok=True)
        _PENDING_OPEN.write_text(json.dumps({"path": pdf_path}, ensure_ascii=False), encoding="utf-8")
        if sys.platform.startswith("win"):
            import ctypes.wintypes as wt

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateEventW.restype = wt.HANDLE
            ev = kernel32.CreateEventW(None, False, False, _OPEN_EVENT_NAME)
            kernel32.SetEvent(ev)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 转交文件失败: {exc}", flush=True)


def watch_open_requests(window, api) -> None:
    """守护线程：等待后续实例的打开请求（仅 Windows）。"""
    if not sys.platform.startswith("win"):
        return

    def _loop() -> None:
        import ctypes.wintypes as wt

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CreateEventW.restype = wt.HANDLE
        kernel32.WaitForSingleObject.restype = wt.DWORD
        ev = kernel32.CreateEventW(None, False, False, _OPEN_EVENT_NAME)
        if not ev:
            return
        while True:
            if kernel32.WaitForSingleObject(ev, 0xFFFFFFFF) != 0:
                continue
            try:
                import json

                data = json.loads(_PENDING_OPEN.read_text(encoding="utf-8"))
                path = data.get("path") or ""
                if path and Path(path).exists():
                    api._open(path)
                    # 前端重启后 get_state 会拿到新文档；保留未保存编辑的
                    # 精细刷新留待后续，reload 是当前最可靠的方式。
                    window.evaluate_js("location.reload()")
                    print(f"[pagesmith] 已转交打开: {path}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] 处理打开请求失败: {exc}", flush=True)

    threading.Thread(target=_loop, daemon=True, name="open-request-watcher").start()


def acquire_single_instance() -> bool:
    """命名互斥量做单实例保护。

    这不是洁癖：WebView2 的用户数据目录是独占的，两个实例同时跑，
    后启动的那个会静默白屏（navigation 不触发、HTTP 一条请求都没有）。
    让第二个实例直接退出，比让用户面对白屏好得多。
    """
    global _MUTEX_HANDLE
    if not sys.platform.startswith("win"):
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 必须声明 argtypes/restype，否则 64 位下句柄被截断成 32 位
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, wintypes.BOOL(True), r"Local\PageSmith.SingleInstance.v1")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _smoke_probe(window) -> None:
    """PDFSTUDIO_SMOKE=1 时启用：等页面画出来，导出 DOM 状态后自动退出。

    用来在没有人工看屏的情况下判断"到底是白屏还是渲染失败"。
    第二阶段还会真实走一遍交互链路：更多菜单 → 编辑模式 → 改一处文字 →
    验证后端与界面同步。任何一步失败都会在输出里留下痕迹。
    """
    import json

    for _ in range(80):
        time.sleep(0.5)
        try:
            if window.evaluate_js("document.readyState") in ("interactive", "complete"):
                break
        except Exception:
            continue
    time.sleep(2.5)
    script = """(() => ({
        title: document.title || '',
        topbar: !!document.querySelector('.topbar'),
        thumbs: document.querySelectorAll('.thumb').length,
        pages: document.querySelectorAll('.page-canvas').length,
        canvases: [...document.querySelectorAll('canvas')].filter(c => c.width > 0 && c.height > 0).length,
        overlayHidden: (() => { const o = document.getElementById('loading'); return !o || o.hidden || getComputedStyle(o).display === 'none'; })(),
        text: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 240)
    }))()"""
    try:
        info = window.evaluate_js(script)
    except Exception as exc:  # noqa: BLE001
        info = {"error": repr(exc)}
    print("[smoke] " + json.dumps(info, ensure_ascii=False), flush=True)

    # ── 第二阶段：交互链路 ──
    try:
        report: dict[str, object] = {}

        def js(expr: str):
            return window.evaluate_js(expr)

        step = lambda name: print(f"[smoke-step] {name}", flush=True)
        step("start")
        # 0) 直接验证 fonts 接口
        js("window.pywebview.api.fonts().then(r => window.__ft = 'OK ' + JSON.stringify(r)).catch(e => window.__ft = 'ERR ' + e)")
        time.sleep(2.5)
        report["fontsCall"] = str(js("window.__ft") or "PENDING")[:40]

        # 0.5) 拖放打开：合成携带真实 PDF 字节的 DropEvent 派发到 document，
        # 走完整「上传 → open_path → 渲染」链路（回归：2026-10 拖入打不开）。
        # 副作用：后续菜单/编辑/水印步骤因此有了已打开的文档。
        try:
            import base64
            import tempfile

            tmp_pdf = Path(tempfile.gettempdir()) / "pagesmith_drop_test.pdf"
            try:
                import pymupdf as _fitz

                _d = _fitz.open()
                _d.new_page()
                _d.save(str(tmp_pdf))
                _d.close()
            except Exception:  # noqa: BLE001 — 兜底：无 xref 的极简 PDF，MuPDF 可修复
                tmp_pdf.write_bytes(
                    b"%PDF-1.4\n"
                    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
                    b"trailer<</Size 4/Root 1 0 R>>\n%%EOF"
                )
            b64 = base64.b64encode(tmp_pdf.read_bytes()).decode()
            js(
                "(async () => {"
                f"const bin = atob('{b64}');"
                "const arr = new Uint8Array(bin.length);"
                "for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);"
                "const file = new File([arr], 'drop_test.pdf', {type: 'application/pdf'});"
                "const dt = new DataTransfer();"
                "dt.items.add(file);"
                "const ev = new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt});"
                "document.dispatchEvent(ev);"
                "})()"
            )
            for _ in range(20):
                time.sleep(0.5)
                if js("document.querySelectorAll('.page-canvas').length"):
                    break
            report["dropOpensPdf"] = int(js("document.querySelectorAll('.page-canvas').length") or 0) > 0
        except Exception as exc:  # noqa: BLE001
            report["dropOpensPdf"] = f"ERR {exc!r}"

        # 1) 更多菜单
        js("document.querySelector('[data-menu=\"more\"]')?.click()")
        time.sleep(0.4)
        report["moreMenuOpens"] = bool(js(
            "!!document.querySelector('.menu[data-menu-for=\"more\"]')?.classList.contains('open')"))
        js("document.body.click()")
        time.sleep(0.2)
        report["moreMenuCloses"] = not bool(js(
            "!!document.querySelector('.menu[data-menu-for=\"more\"]')?.classList.contains('open')"))
        # 1c) 「设为默认 PDF 工具」菜单项必须存在（不真点，避免改用户注册表）
        report["defaultPdfMenuItem"] = bool(js("!!document.getElementById('act-set-default-pdf')"))
        # 1d) 打印渲染接口：从页面内 fetch 一张 150dpi 页图，确认字节数合理
        js("fetch('api/print/0?dpi=150').then(r => r.blob()).then(b => window.__pt = 'OK ' + b.size)"
           ".catch(e => window.__pt = 'ERR ' + e)")
        time.sleep(3.0)
        report["printEndpoint"] = str(js("window.__pt") or "PENDING")[:30]

        step("more-menu")
        # 1b) 页面 / 转换 / 优化 三个菜单触发器必须已启用并能打开
        report["menuTriggersEnabled"] = int(js(
            "document.querySelectorAll('[data-menu]:not([disabled])').length") or 0)
        js("document.querySelector('[data-menu=\"pages\"]')?.click()")
        time.sleep(0.4)
        report["pagesMenuOpens"] = bool(js(
            "!!document.querySelector('.menu[data-menu-for=\"pages\"]')?.classList.contains('open')"))
        js("document.body.click()")
        time.sleep(0.2)
        js("document.querySelector('[data-menu=\"convert\"]')?.click()")
        time.sleep(0.4)
        report["convertMenuOpens"] = bool(js(
            "!!document.querySelector('.menu[data-menu-for=\"convert\"]')?.classList.contains('open')"))
        js("document.body.click()")
        time.sleep(0.2)
        js("document.querySelector('[data-menu=\"opt\"]')?.click()")
        time.sleep(0.4)
        report["optMenuOpens"] = bool(js(
            "!!document.querySelector('.menu[data-menu-for=\"opt\"]')?.classList.contains('open')"))
        js("document.body.click()")
        time.sleep(0.2)
        # 2) 进入编辑模式
        js("document.querySelector('[data-act=\"edit-text\"]')?.click()")
        time.sleep(2.0)
        report["editbarVisible"] = bool(js(
            "document.getElementById('editbar') && !document.getElementById('editbar').hidden"))
        report["editSpans"] = int(js("document.querySelectorAll('.edit-span').length") or 0)

        # 点一个文本块，应弹出行内编辑器
        js("document.querySelector('.edit-span')?.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        time.sleep(0.8)
        report["inlineEditor"] = bool(js("!!document.querySelector('.edit-textarea')"))
        js("document.querySelector('.edit-textarea')?.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        time.sleep(0.3)

        step("edit-mode")
        # 3) 取第一段文字并替换
        js("window.pywebview.api.text_spans(0).then(r => window.__spans = JSON.stringify(r))")
        time.sleep(1.0)
        spans_raw = js("window.__spans") or "{}"
        envelope = json.loads(spans_raw)
        spans = (envelope.get("data") if isinstance(envelope, dict) else envelope) or []
        if spans:
            target = spans[0]
            payload = json.dumps({
                "kind": "replace", "page": 0, "rect": target["bbox"],
                "text": "已编辑 OK", "size": target["size"],
                "font": "msyh", "color": "#111111", "direction": "h",
            }, ensure_ascii=False)
            js("window.pywebview.api.apply_edit(%s).then(r => window.__edit = JSON.stringify(r)).catch(e => window.__edit = 'ERR ' + e)" % payload)
            time.sleep(3.5)
            result = js("window.__edit")
            if result and not result.startswith("ERR"):
                js("window.pywebview.api.text_spans(0).then(r => window.__after = JSON.stringify(r))")
                time.sleep(1.0)
                after = json.loads(js("window.__after") or "{}")
                after_spans = after.get("data") if isinstance(after, dict) else after
                joined = " ".join(s["text"] for s in (after_spans or []))
                report["editApplied"] = "已编辑 OK" in joined
                report["editSpansAfterRerender"] = int(js("document.querySelectorAll('.edit-span').length") or 0)
                # 撤销，让重复运行的结果可复现
                js("window.pywebview.api.undo_edit()")
                time.sleep(1.0)
            else:
                report["editApplied"] = f"FAILED: {result}"
        else:
            report["editApplied"] = "NO SPANS"

        step("edit-apply")
        # 4) 水印：优化菜单 → 打开对话框 → 等预览 → 改角度 → 应用 → 撤销
        js("document.querySelector('[data-menu=\"opt\"]')?.click()")
        time.sleep(0.4)
        js("document.querySelector('.menu[data-menu-for=\"opt\"] [data-act=\"watermark\"]')?.click()")
        step("wm-menu")
        # 点水印入口，轮询等弹窗出现（最多 8s）
        t0 = time.time()
        opened = False
        for _ in range(16):
            if js("!document.getElementById('modal-mask').hidden"):
                opened = True
                break
            time.sleep(0.5)
        report["wmDialogOpen"] = opened
        report["wmDialogDelay"] = round(time.time() - t0, 1)
        if opened:
            for _ in range(24):  # 等首帧预览（防抖 260ms + 渲染）
                time.sleep(0.5)
                if js("const i = document.getElementById('wm-img'); i && !i.hidden && i.src.length > 100"):
                    break
            step("wm-dialog")
            report["wmPreview"] = bool(js(
                "const i = document.getElementById('wm-img'); !!i && !i.hidden && i.src.length > 100"))
            # 改角度（360°滑块）+ 平铺，应触发第二次预览
            js("const a = document.getElementById('wm-angle'); a.value = 120;"
               "a.dispatchEvent(new Event('input', {bubbles: true}))")
            time.sleep(1.8)
            step("wm-preview1")
            report["wmAngleLabel"] = js("document.getElementById('wm-angle-val')?.textContent")
            js("document.querySelector('#wm-mode [data-mode=\"tile\"]')?.click()")
            time.sleep(1.8)
            step("wm-preview2")
            # 应用
            js("[...document.querySelectorAll('#modal-foot .btn-primary')].pop()?.click()")
            time.sleep(4.0)
            step("wm-apply")
            report["wmDialogClosed"] = bool(js("document.getElementById('modal-mask')?.hidden"))
            report["wmDirtyFlag"] = not js("document.getElementById('st-dirty')?.hidden")
            # 水印是真实文字：确认已写进内容流
            js("window.pywebview.api.doc_text(0).then(r => window.__wmtext = JSON.stringify(r))")
            time.sleep(1.2)
            wm_text_raw = json.loads(js("window.__wmtext") or "{}")
            wm_text = (wm_text_raw.get("data") if isinstance(wm_text_raw, dict) else wm_text_raw) or ""
            report["wmTextWritten"] = "内部资料" in str(wm_text)
            if os.environ.get("PDFSTUDIO_SMOKE_APPLY_HOLD"):
                # 保持已加水印的状态，供外部截图
                print("[smoke] hold applied-watermark state", flush=True)
            else:
                js("window.pywebview.api.undo_edit()")
                time.sleep(1.5)

        print("[smoke-ui] " + json.dumps(report, ensure_ascii=False), flush=True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[smoke-ui] error {exc!r}", flush=True)

    if os.environ.get("PDFSTUDIO_SMOKE_STAY"):
        if os.environ.get("PDFSTUDIO_SMOKE_MENU_SHOT"):
            # 把某个下拉菜单停住，供外部截图（pages / convert / opt）
            name = os.environ["PDFSTUDIO_SMOKE_MENU_SHOT"]
            js(f"document.querySelector('[data-menu=\"{name}\"]')?.click()")
            time.sleep(4.0)
            print(f"[smoke] menu '{name}' on screen", flush=True)
        if os.environ.get("PDFSTUDIO_SMOKE_WM_SHOT"):
            # 把水印对话框停在屏幕上，供外部截图
            js("document.querySelector('[data-menu=\"opt\"]')?.click()")
            time.sleep(0.4)
            js("document.querySelector('.menu[data-menu-for=\"opt\"] [data-act=\"watermark\"]')?.click()")
            time.sleep(5.0)
            print("[smoke] watermark dialog on screen", flush=True)
        # 供外部截图观察界面状态，进程由外部结束
        print("[smoke] stay=1，窗口保持打开", flush=True)
        while True:
            time.sleep(3600)

    try:
        window.destroy()
    except Exception:
        os._exit(0)


def main() -> int:
    cli_pdf = next((a for a in sys.argv[1:] if a.lower().endswith(".pdf") and Path(a).exists()), None)

    if not acquire_single_instance():
        print("[pagesmith] 已有一个实例在运行，本次启动退出。", flush=True)
        if cli_pdf:
            signal_existing_instance(cli_pdf)
        return 2

    session = DocumentSession()
    web_root = find_web_root()
    server = LocalServer(web_root, session)
    server.start()

    api = Api(session, web_root)
    window = webview.create_window(
        f"{APP_NAME} {VERSION}",
        server.url,
        js_api=api,
        width=1320,
        height=880,
        min_size=(980, 640),
        background_color="#f4f5f7",
    )
    api._window = window

    # 命令行直接带文件打开（会话与窗口解耦，先后皆可）
    if cli_pdf:
        try:
            api._open(cli_pdf)
            print(f"[pagesmith] 预打开: {cli_pdf}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 无法预打开 {cli_pdf}: {exc}", flush=True)

    # 后续实例（双击 PDF）转交打开请求
    watch_open_requests(window, api)

    debug = bool(os.environ.get("PDFSTUDIO_DEBUG"))
    smoke = bool(os.environ.get("PDFSTUDIO_SMOKE"))
    print(f"[pagesmith] {server.url}  debug={debug} smoke={smoke}", flush=True)

    try:
        # 注意：不要在这里额外传 debug= / http_server= —— 实测 pywebview 6.2.1
        # 显式传这些参数（即便值与默认相同）会让 WebView2 的导航静默失败。
        # 调试期用环境变量 PDFSTUDIO_DEBUG 控制 devtools。
        webview.start(
            (_smoke_probe if smoke else None),
            window,
            private_mode=False,
            storage_path=prepare_storage(),
        )
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
