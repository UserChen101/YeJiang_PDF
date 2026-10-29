"""暴露给前端的接口层（通过 pywebview 的 js_api 通道调用）。

约定
----
* 每个方法都返回 {"ok": bool, "data": ..., "error": str}，前端只需要判 ok。
* 所有异常在这里被翻译成人能看懂的中文，绝不让堆栈泄漏到界面上。
* 文件选择一律走系统原生对话框，路径不经过浏览器沙箱，避免各种权限坑。
"""

from __future__ import annotations

import base64
import functools
import os
import platform
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

import webview

from .core import compress as ccomp
from .core import convert as cconv
from .core import edit as cedit
from .core import ocr as cocr
from .core.doc import DocumentSession

if sys.platform.startswith("win"):
    from .core import assoc as cassoc

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def action(fn: Callable) -> Callable:
    """统一的返回包装与异常兜底。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            data = fn(self, *args, **kwargs)
            return {"ok": True, "data": data}
        except (cconv.ConversionError, cocr.OcrUnavailable) as exc:
            return {"ok": False, "error": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "error": f"权限不足：{exc}"}
        except FileNotFoundError as exc:
            return {"ok": False, "error": f"文件不存在：{exc}"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return wrapper


class _PrintBridge:
    """打印预览窗口的 js_api 桥：预览页打印完成/取消后回调关闭自己。"""

    def __init__(self) -> None:
        self._wnd = None  # 下划线前缀，避免 js_api 递归序列化

    def bind(self, wnd) -> None:
        self._wnd = wnd

    def print_done(self) -> dict:
        wnd, self._wnd = self._wnd, None
        if wnd is not None:
            try:
                wnd.destroy()
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True}


class Api:
    def __init__(self, session: DocumentSession, web_root: Path, server=None) -> None:
        self._session = session
        self._web_root = web_root
        self._server = server  # LocalServer，用于拼打印预览页地址；勿改名（js_api 递归）
        self._window: webview.Window | None = None
        self._print_bridge: _PrintBridge | None = None
        self.recent: list[dict] = []
        self.prefs_path = Path.home() / ".pagesmith" / "prefs.json"
        self._load_prefs()

    # ------------------------------------------------------------ 偏好存储

    def _load_prefs(self) -> None:
        import json

        try:
            if self.prefs_path.exists():
                data = json.loads(self.prefs_path.read_text("utf-8"))
                self.recent = data.get("recent", [])[:20]
        except Exception:
            self.recent = []

    def _save_prefs(self) -> None:
        import json

        try:
            self.prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self.prefs_path.write_text(
                json.dumps({"recent": self.recent[:20]}, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            pass

    def _push_recent(self, meta: dict) -> None:
        if not meta.get("path"):
            return
        self.recent = [r for r in self.recent if r.get("path") != meta["path"]]
        self.recent.insert(
            0,
            {
                "path": meta["path"],
                "name": meta["name"],
                "pages": meta["pages"],
                "at": __import__("time").time(),
            },
        )
        self._save_prefs()

    # ------------------------------------------------------------ 对话框

    def _dialog_types(self, kind: str) -> tuple[str, ...]:
        if IS_MAC:
            return {
                "pdf": ("PDF", "*.pdf"),
                "img": ("图片", "*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff"),
                "doc": ("Word 文档", "*.docx;*.doc"),
            }.get(kind, ("所有文件", "*.*"))
        return {
            "pdf": ("PDF 文件 (*.pdf)", "所有文件 (*.*)"),
            "img": ("图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff)", "所有文件 (*.*)"),
            "doc": ("Word 文档 (*.docx;*.doc)", "所有文件 (*.*)"),
        }.get(kind, ("所有文件 (*.*)",))

    def _norm_dialog(self, result: Any) -> list[str]:
        if not result:
            return []
        if isinstance(result, str):
            return [result]
        return [str(p) for p in result if p]

    # ------------------------------------------------------------ 基础信息

    @action
    def get_state(self) -> dict:
        if self._session.doc is None:
            return {"empty": True, "recent": self.recent, "ocr": cocr.available()}
        meta = self._session.meta()
        meta["empty"] = False
        meta["recent"] = self.recent
        meta["ocr"] = cocr.available()
        meta["outline"] = self._session.outline()
        return meta

    @action
    def open_dialog(self) -> dict:
        files = self._norm_dialog(
            self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=self._dialog_types("pdf"))
        )
        if not files:
            return {"cancelled": True}
        path = files[0]
        try:
            return self._open(path)
        except PermissionError:
            # 交给前端弹密码框，避免直接报错丢掉文件路径
            return {"need_password": True, "path": path, "name": Path(path).name}

    @action
    def open_path(self, path: str, password: str = "") -> dict:
        return self._open(path, password or None)

    def _open(self, path: str, password: str | None = None) -> dict:
        meta = self._session.open(path, password)
        meta["empty"] = False
        meta["outline"] = self._session.outline()
        meta["recent"] = self.recent
        meta["ocr"] = cocr.available()
        self._push_recent(meta)
        return meta

    # ---------------------------------------------------- 默认 PDF 打开工具

    @action
    def pdf_assoc_status(self) -> dict:
        """查询当前关联状态：是否已注册进系统候选、是否已是默认。"""
        if not IS_WIN:
            return {"supported": False}
        return {
            "supported": True,
            "registered": cassoc.is_registered(),
            "is_default": cassoc.is_default(),
        }

    @action
    def set_default_pdf_app(self) -> dict:
        """注册进系统候选并尝试直接设为默认。

        Windows 对 UserChoice 有哈希保护，不同版本策略不同：COM 直设
        可能被拒。返回 applied=False 时前端自动打开系统设置页，
        用户点一下「设为默认」即可（注册项已经就位，候选列表里就有页匠）。
        """
        if not IS_WIN:
            raise RuntimeError("此功能仅支持 Windows")
        cassoc.register()
        applied = cassoc.try_set_default()
        return {
            "applied": applied,
            "is_default": cassoc.is_default(),
            "settings_url": None if applied else cassoc.settings_url(),
        }

    @action
    def unset_default_pdf_app(self) -> dict:
        """从系统候选里移除注册。若此前已是默认，系统会在下次打开时回退。"""
        if not IS_WIN:
            raise RuntimeError("此功能仅支持 Windows")
        cassoc.unregister()
        return {"removed": True}

    @action
    def open_settings_page(self) -> dict:
        """打开系统「默认应用」设置页（ms-settings URI）。"""
        if not IS_WIN:
            raise RuntimeError("此功能仅支持 Windows")
        os.startfile(cassoc.settings_url())  # noqa: S606 — 仅系统 URI，无注入面
        return {"opened": True}


    @action
    def unlock(self, path: str, password: str) -> dict:
        return self._open(path, password)

    @action
    def close_doc(self) -> dict:
        self._session.close()
        return {"empty": True, "recent": self.recent, "ocr": cocr.available()}

    @action
    def page_sizes(self) -> list:
        return self._session.page_sizes()

    @action
    def set_restore_page(self, page: int) -> dict:
        self._session._restore_page = max(0, int(page))
        return {"ok": True}

    @action
    def doc_text(self, page: int | None = None) -> str:
        if page is None:
            return self._session.all_text()
        return self._session.page_text(int(page))

    # ------------------------------------------------------------ 文字编辑

    @action
    def fonts(self) -> list:
        return cedit.available_fonts()

    @action
    def text_spans(self, page: int) -> list:
        return self._session.text_spans(int(page))

    @action
    def apply_edit(self, payload: dict) -> dict:
        return self._session.apply_edit(payload)

    @action
    def undo_edit(self) -> dict:
        return self._session.undo_edit()

    # ------------------------------------------------------------ 保存

    @action
    def save_as_dialog(self, suggested: str = "", password: str = "", owner_password: str = "") -> dict:
        default = suggested or (self._session.path.name if self._session.path else "document.pdf")
        target = self._window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
            save_filename=default,
            file_types=self._dialog_types("pdf"),
        )
        target = self._norm_dialog(target)
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        path = self._session.save(out, password=password or None, owner_password=owner_password or None)
        self._push_recent(self._session.meta())
        return {"path": path, "meta": self._session.meta()}

    @action
    def save(self) -> dict:
        if not self._session.path:
            return {"need_path": True}
        meta = self._session.save_inplace()
        return {"path": meta.get("path"), "meta": meta}

    # ------------------------------------------------------------ 页面操作

    @action
    def rotate(self, pages: Any = None, angle: int = 90) -> dict:
        return self._session.rotate(pages, int(angle))

    @action
    def delete_pages(self, pages: Any) -> dict:
        return self._session.delete_pages(pages)

    @action
    def move_page(self, src: int, dst: int) -> dict:
        return self._session.move_page(int(src), int(dst))

    @action
    def reverse(self) -> dict:
        return self._session.reverse()

    @action
    def blank_page(self, at: int | None = None) -> dict:
        return self._session.blank_page(at)

    @action
    def insert_pdf_dialog(self) -> dict:
        files = self._norm_dialog(
            self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=self._dialog_types("pdf")
            )
        )
        if not files:
            return {"cancelled": True}
        meta = None
        for f in files:
            meta = self._session.insert_pdf(f)
        return meta or self._session.meta()

    @action
    def insert_images_dialog(self) -> dict:
        files = self._norm_dialog(
            self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=self._dialog_types("img")
            )
        )
        if not files:
            return {"cancelled": True}
        tmp = Path(tempfile.mkdtemp()) / "imgs.pdf"
        cconv.images_to_pdf(files, str(tmp), page_size="a4", margin=18)
        return self._session.insert_pdf(str(tmp))

    @action
    def extract_dialog(self, pages: Any) -> dict:
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
                save_filename="extracted.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        return {"path": self._session.extract_pages(pages, out)}

    @action
    def split_dialog(self, every: int = 1) -> dict:
        folder = self._norm_dialog(self._window.create_file_dialog(webview.FOLDER_DIALOG))
        if not folder:
            return {"cancelled": True}
        files = self._session.split_every(int(every), folder[0])
        return {"files": files, "count": len(files)}

    @action
    def merge_dialog(self) -> dict:
        files = self._norm_dialog(
            self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=self._dialog_types("pdf")
            )
        )
        if not files:
            return {"cancelled": True}
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path(files[0]).parent),
                save_filename="merged.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        cconv.merge_files(files, out)
        return self._open(out)

    # ------------------------------------------------------------ 注释

    @action
    def add_annotation(self, payload: dict) -> dict:
        return self._session.add_annotation(payload)

    @action
    def remove_annotation(self, aid: str) -> bool:
        return self._session.remove_annotation(aid)

    @action
    def update_annotation(self, aid: str, content: str) -> bool:
        return self._session.update_annotation(aid, content)

    @action
    def clear_annotations(self) -> dict:
        self._session.clear_annotations()
        return self._session.meta()

    @action
    def burn_annotations(self) -> dict:
        """把注释真正写进文件（另存一份，不覆盖原件）。"""
        if not self._session.path:
            return {"error": "请先另存为"}
        out = self._session.path.with_name(f"{self._session.path.stem}_已批注.pdf")
        self._session.save(str(out))
        return {"path": str(out)}

    # ------------------------------------------------------------ 转换

    @action
    def convert_dialog(self, kind: str, pages: Any = None, dpi: int = 150, fmt: str = "png", quality: int = 88) -> dict:
        if self._session.doc is None:
            raise RuntimeError("请先打开一个 PDF")

        if kind == "images":
            folder = self._norm_dialog(self._window.create_file_dialog(webview.FOLDER_DIALOG))
            if not folder:
                return {"cancelled": True}
            files = cconv.pdf_to_images(self._session, folder[0], fmt=fmt, dpi=int(dpi), pages=pages, quality=int(quality))
            return {"files": files, "count": len(files)}

        ext_map = {"docx": ".docx", "xlsx": ".xlsx", "txt": ".txt", "html": ".html", "csv": ".csv"}
        ext = ext_map.get(kind, ".pdf")
        stem = self._session.path.stem if self._session.path else "document"
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
                save_filename=f"{stem}{ext}",
                file_types=(f"{ext.upper().lstrip('.')} 文件 (*{ext})", "所有文件 (*.*)"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(ext):
            out += ext

        if kind == "docx":
            cconv.pdf_to_docx(self._session, out, pages)
            return {"path": out}
        if kind == "xlsx":
            return cconv.pdf_to_excel(self._session, out, pages)
        if kind == "txt":
            cconv.pdf_to_text(self._session, out, pages)
            return {"path": out}
        if kind == "html":
            cconv.pdf_to_html(self._session, out, pages)
            return {"path": out}
        if kind == "csv":
            folder = self._norm_dialog(self._window.create_file_dialog(webview.FOLDER_DIALOG))
            if not folder:
                return {"cancelled": True}
            files = cconv.pdf_to_csv(self._session, folder[0], pages)
            return {"files": files, "count": len(files)}
        raise RuntimeError(f"不支持的格式：{kind}")

    @action
    def files_to_pdf_dialog(self, kind: str = "img") -> dict:
        files = self._norm_dialog(
            self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True, file_types=self._dialog_types(kind)
            )
        )
        if not files:
            return {"cancelled": True}
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path(files[0]).parent),
                save_filename="converted.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        if kind == "doc":
            cconv.docx_to_pdf(files[0], out)
        else:
            cconv.images_to_pdf(files, out)
        return self._open(out)

    # ------------------------------------------------------------ OCR

    @action
    def ocr(self, pages: Any = None, dpi: int = 300) -> dict:
        return cocr.ocr_pages(self._session, pages=pages, dpi=int(dpi), write_layer=True)

    @action
    def ocr_text(self, pages: Any = None) -> dict:
        text = cocr.ocr_text_only(self._session, pages=pages)
        return {"text": text, "chars": len(text)}

    # ------------------------------------------------------------ 压缩 / 安全

    @action
    def compress(self, level: str = "medium", keep_original: bool = True) -> dict:
        out = None
        if keep_original and self._session.path:
            out = str(self._session.path.with_name(f"{self._session.path.stem}_压缩.pdf"))
        elif not keep_original:
            out = str(self._session.path) if self._session.path else None
        return ccomp.compress(self._session, level=level, out_path=out)

    @action
    def compress_as_dialog(self, level: str = "medium") -> dict:
        stem = self._session.path.stem if self._session.path else "document"
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
                save_filename=f"{stem}_压缩.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        return ccomp.compress(self._session, level=level, out_path=out)

    @action
    def encrypt_dialog(self, user_pw: str, owner_pw: str = "", allow_print: bool = True, allow_copy: bool = True) -> dict:
        if not user_pw:
            raise ValueError("打开密码不能为空")
        stem = self._session.path.stem if self._session.path else "document"
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
                save_filename=f"{stem}_加密.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        ccomp.encrypt(self._session, out, user_pw, owner_pw, allow_print, allow_copy)
        return {"path": out}

    @action
    def decrypt_dialog(self) -> dict:
        stem = self._session.path.stem if self._session.path else "document"
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(self._session.path.parent) if self._session.path else str(Path.home()),
                save_filename=f"{stem}_解密.pdf",
                file_types=self._dialog_types("pdf"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        ccomp.decrypt(self._session, out)
        return {"path": out}

    @action
    def watermark(self, payload: dict) -> dict:
        """添加水印。payload：{text, size, angle(0-360), opacity, color, font, tile, pages}"""
        return self._session.watermark(payload)

    @action
    def watermark_preview(self, payload: dict) -> dict:
        """渲染水印预览图（data URL），不改动文档。"""
        return self._session.watermark_preview(payload)

    @action
    def set_metadata(self, data: dict) -> dict:
        return self._session.set_metadata(data)

    # ------------------------------------------------------------ 系统集成

    @action
    def reveal_in_explorer(self, path: str = "") -> dict:
        target = path or (str(self._session.path) if self._session.path else "")
        if not target:
            return {"ok": False}
        p = Path(target)
        if IS_WIN:
            subprocess.Popen(["explorer", "/select,", str(p)])
        elif IS_MAC:
            subprocess.Popen(["open", "-R", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent)])
        return {"ok": True}

    @action
    def open_print_window(self) -> dict:
        """打开独立的「打印预览」窗口并在其中调起系统打印对话框。

        绝不能在主窗口调用 window.print()：WebView2 关闭打印对话框后
        可能停止刷新主窗口画面（已知渲染冻结问题，实测表现为界面
        永远停留在「正在准备打印」）。独立窗口用完即毁，主窗口零风险。
        """
        if self._server is None or not self._server.port:
            raise RuntimeError("本地服务未就绪")
        if self._session.doc is None:
            raise RuntimeError("请先打开一个 PDF")
        self._print_bridge = _PrintBridge()
        bridge = self._print_bridge
        url = (
            f"http://127.0.0.1:{self._server.port}"
            f"/printview?t={self._session.token}&dpi=150"
        )
        wnd = webview.create_window(
            "打印预览 - 页匠 PageSmith",
            url,
            js_api=bridge,
            width=880,
            height=1000,
            min_size=(560, 420),
            background_color="#525659",
        )
        bridge.bind(wnd)
        return {"ok": True}
        return {"ok": True}

    @action
    def open_external(self, path: str = "") -> dict:
        target = path or (str(self._session.path) if self._session.path else "")
        if not target:
            return {"ok": False}
        if IS_WIN:
            os.startfile(target)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return {"ok": True}

    @action
    def copy_text(self, text: str) -> dict:
        if self._window:
            self._window.evaluate_js(
                "navigator.clipboard.writeText(%s).catch(()=>{})" % __import__("json").dumps(text)
            )
        return {"ok": True}

    @action
    def pick_folder(self) -> dict:
        folder = self._norm_dialog(self._window.create_file_dialog(webview.FOLDER_DIALOG))
        if not folder:
            return {"cancelled": True}
        return {"path": folder[0]}

    @action
    def save_text_dialog(self, text: str, suggested: str = "export.txt") -> dict:
        target = self._norm_dialog(
            self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=suggested,
                file_types=("文本文件 (*.txt)", "所有文件 (*.*)"),
            )
        )
        if not target:
            return {"cancelled": True}
        out = target[0]
        Path(out).write_text(text, encoding="utf-8")
        return {"path": out}

    @action
    def app_info(self) -> dict:
        return {
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "frozen": bool(getattr(sys, "frozen", False)),
            "ocr": cocr.available(),
        }
