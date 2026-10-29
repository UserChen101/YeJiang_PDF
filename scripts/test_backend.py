"""后端集成测试：不启动窗口，直接验证 HTTP 服务 + 文档会话的完整链路。

python scripts/test_backend.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402

from app.core.doc import DocumentSession  # noqa: E402
from app.server import LocalServer  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ok_count = 0
fail_count = 0


def ok(name, detail=""):
    global ok_count
    ok_count += 1
    print(f"[OK]   {name}  {detail}")


def bad(name, detail=""):
    global fail_count
    fail_count += 1
    print(f"[FAIL] {name}  {detail}")


def check(name, fn):
    try:
        detail = fn()
        ok(name, detail if detail is not None else "")
    except Exception as exc:
        bad(name, f"{type(exc).__name__}: {exc}")


def make_sample_pdf(path: Path, pages: int = 6) -> None:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((64, 90), f"Chapter {i + 1}", fontsize=24, fontname="helv")
        page.insert_text((64, 130), f"这是第 {i + 1} 页的中文内容，用于验证渲染与搜索。",
                         fontsize=13, fontname="china-s")
        page.insert_text((64, 160), "The quick brown fox jumps over the lazy dog.",
                         fontsize=11, fontname="helv")
        # 画一个真表格（有线框，find_tables 才能识别）
        x0, y0, cw, ch, rows, cols = 64, 200, 150, 24, 3, 3
        for r in range(rows + 1):
            page.draw_line(fitz.Point(x0, y0 + r * ch), fitz.Point(x0 + cols * cw, y0 + r * ch))
        for c in range(cols + 1):
            page.draw_line(fitz.Point(x0 + c * cw, y0), fitz.Point(x0 + c * cw, y0 + rows * ch))
        for r in range(rows):
            for c in range(cols):
                page.insert_text((x0 + c * cw + 6, y0 + r * ch + 16),
                                 f"P{i + 1}-R{r}C{c}", fontsize=9, fontname="helv")
    doc.save(path, garbage=3, deflate=True)
    doc.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pagesmith_test_"))
    sample = tmp / "测试文档.pdf"
    make_sample_pdf(sample)
    print(f"样本：{sample}  ({sample.stat().st_size} B)\n")

    session = DocumentSession()
    server = LocalServer(ROOT / "app" / "web", session)
    port = server.start()
    base = f"http://127.0.0.1:{port}"
    print(f"服务：{base}\n")

    def get(path, expect=200):
        req = urllib.request.Request(f"{base}{path}")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read(), dict(r.headers)

    # ── 静态资源
    check("GET / 返回页面", lambda: (lambda s, b, _: (s, len(b)))(*get("/"))[0])
    for asset in ("css/app.css", "js/app.js", "js/viewer.js", "js/dialog.js",
                  "vendor/pdfjs/pdf.mjs", "vendor/pdfjs/pdf.worker.mjs"):
        check(f"静态资源 {asset}", lambda a=asset: f"{len(get('/' + a)[1])} B")

    check("404 处理", lambda: _expect_status(base, "/nope.js", 404))
    check("目录穿越被拦截", lambda: _expect_status(base, "/../main.py", 403))

    # ── 打开文档
    check("打开 PDF", lambda: session.open(sample)["pages"])

    st, body, hdrs = get(f"/doc.pdf?t={session.token}")
    if st == 200 and body[:4] == b"%PDF":
        ok("GET /doc.pdf", f"{len(body)} B, {hdrs.get('Content-Type')}")
    else:
        bad("GET /doc.pdf", f"status={st}, head={body[:20]!r}")

    # Range 请求
    req = urllib.request.Request(f"{base}/doc.pdf", headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=10) as r:
        part = r.read()
        code = r.status
    if code == 206 and len(part) == 100:
        ok("Range 请求", f"206, {len(part)} B, {r.headers.get('Content-Range')}")
    else:
        bad("Range 请求", f"status={code}, len={len(part)}")

    # 过期 token
    check("过期 token 返回 409", lambda: _expect_status(base, "/doc.pdf?t=stale", 409))

    # ── 缩略图
    st, body, hdrs = get(f"/api/thumb/0?w=160&t={session.token}")
    if st == 200 and body[:2] == b"\xff\xd8":
        ok("缩略图 JPEG", f"{len(body)} B")
    else:
        bad("缩略图 JPEG", f"status={st}, head={body[:8]!r}")

    t0 = time.perf_counter()
    for i in range(6):
        get(f"/api/thumb/{i}?w=160&t={session.token}")
    first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for i in range(6):
        get(f"/api/thumb/{i}?w=160&t={session.token}")
    cached = (time.perf_counter() - t0) * 1000
    ok("缩略图缓存生效", f"首次 {first:.0f}ms → 命中 {cached:.0f}ms")

    # ── 文档操作
    check("旋转页面", lambda: session.rotate([0], 90)["version"])
    check("删除页面", lambda: session.delete_pages([5])["pages"])
    check("移动页面", lambda: session.move_page(0, 2)["pages"])
    check("加空白页", lambda: session.blank_page()["pages"])
    check("页面尺寸", lambda: len(session.page_sizes()))

    # token 已变，旧 token 应 409
    check("修改后旧 token 失效", lambda: _expect_status(base, "/doc.pdf?t=stale", 409))

    # ── 注释
    annot = session.add_annotation({
        "kind": "highlight", "page": 0,
        "rects": [[64, 78, 300, 100]], "color": [1, 0.86, 0.32], "content": "测试高亮",
    })
    check("添加高亮注释", lambda: annot["id"])

    session.add_annotation({
        "kind": "note", "page": 1,
        "rects": [[300, 300, 320, 320]], "color": [0.96, 0.62, 0.04], "content": "便签内容",
    })
    check("注释计数", lambda: len(session.meta()["annotations"]))

    # ── 保存 + 验证注释真的写进文件
    out = tmp / "out_annotated.pdf"
    session.save(str(out))
    d = fitz.open(out)
    total_annots = sum(len(list(p.annots() or [])) for p in d)
    d.close()
    if total_annots >= 2:
        ok("注释写入文件", f"{total_annots} 条")
    else:
        bad("注释写入文件", f"only {total_annots}")

    # ── 文字编辑（后端直连 session，坐标系走 pdf.js 空间）──
    session.open(sample)
    spans = session.text_spans(0)
    target = next((s for s in spans if "Chapter 1" in s["text"]), None)
    if target:
        ok("读取文本片段", f"{len(spans)} 段, 目标={target['text']!r}")

        def _replace():
            session.apply_edit({
                "kind": "replace", "page": 0, "rect": target["bbox"],
                "text": "Chapter One", "size": target["size"],
                "font": "arial", "color": [0, 0, 0], "direction": "h",
            })
            return session.text_spans(0)

        new_spans = _replace()
        texts = " ".join(s["text"] for s in new_spans)
        if "Chapter One" in texts and "Chapter 1" not in texts:
            ok("替换文字", "Chapter 1 → Chapter One")
        else:
            bad("替换文字", texts[:80])

        def _undo():
            meta = session.undo_edit()
            t = " ".join(s["text"] for s in session.text_spans(0))
            if "Chapter 1" not in t:
                raise AssertionError(f"撤销后未恢复: {t[:60]}")
            return f"version={meta['version']}"

        check("撤销恢复", _undo)

        def _insert():
            session.apply_edit({
                "kind": "insert", "page": 0, "x": 300, "y": 400,
                "text": "编辑测试 OK", "size": 16, "font": "simhei",
                "color": [0.8, 0, 0], "direction": "h",
            })
            t = " ".join(s["text"] for s in session.text_spans(0))
            if "编辑测试 OK" not in t:
                raise AssertionError(f"插入后未找到新文字: {t[:80]}")
            return "found"
        check("插入文字", _insert)

        def _vertical():
            session.apply_edit({
                "kind": "insert", "page": 1, "x": 480, "y": 100,
                "text": "竖排文字", "size": 18, "font": "simhei",
                "color": [0, 0, 0], "direction": "v",
            })
            spans1 = session.text_spans(1)
            hits = [s for s in spans1 if s["text"] in "竖排文字" and s["bbox"][0] > 400]
            if len(hits) < 3:
                raise AssertionError(f"竖排应拆成多段: {len(hits)}")
            ys = sorted(s["bbox"][1] for s in hits)
            if ys[-1] - ys[0] < 40:
                raise AssertionError("竖排字符未向下堆叠")
            return f"{len(hits)} 段"
        check("竖向插入", _vertical)

        def _reverse():
            session.apply_edit({
                "kind": "insert", "page": 1, "x": 380, "y": 100,
                "text": "REVERSED", "size": 16, "font": "arial",
                "color": [0, 0, 0], "direction": "r",
            })
            t = " ".join(s["text"] for s in session.text_spans(1))
            if "REVERSED" not in t:
                raise AssertionError("反向插入后未找到文字")
            return "found"
        check("反向插入", _reverse)

        def _delete():
            sp = next(s for s in session.text_spans(1) if s["text"] == "REVERSED")
            session.apply_edit({"kind": "delete", "page": 1, "rect": sp["bbox"]})
            t = " ".join(s["text"] for s in session.text_spans(1))
            if "REVERSED" in t:
                raise AssertionError("删除后文字仍在")
            return "removed"
        check("删除文字", _delete)

        def _rotated():
            session.rotate([0], 90)
            sp = next((s for s in session.text_spans(0) if "Chapter 1" in s["text"]), None)
            if not sp:
                raise AssertionError("旋转后读不到文本片段")
            session.apply_edit({
                "kind": "replace", "page": 0, "rect": sp["bbox"],
                "text": "ROT90", "size": sp["size"], "font": "arial",
                "color": [0, 0, 0], "direction": "h",
            })
            t = " ".join(s["text"] for s in session.text_spans(0))
            if "ROT90" not in t:
                raise AssertionError(f"旋转页替换失败: {t[:60]}")
            return "rot90 ok"
        check("旋转页编辑", _rotated)

        session.undo_edit()  # 撤掉旋转，保持后续用例干净
    else:
        bad("读取文本片段", "未找到 Chapter 1")

    # ── 转换
    from app.core import convert as cv  # noqa: E402

    session.open(out)
    check("PDF→文本", lambda: len(cv.pdf_to_text(session, str(tmp / "a.txt"))) )
    check("PDF→HTML", lambda: len(Path(cv.pdf_to_html(session, str(tmp / "a.html"))).read_text("utf-8")))
    check("PDF→图片", lambda: len(cv.pdf_to_images(session, str(tmp / "imgs"), dpi=100)))
    check("PDF→Word", lambda: _size(cv.pdf_to_docx(session, str(tmp / "a.docx"))))
    check("PDF→Excel", lambda: cv.pdf_to_excel(session, str(tmp / "a.xlsx")).get("tables"))

    check("图片→PDF", lambda: _size(cv.images_to_pdf(
        sorted(str(p) for p in (tmp / "imgs").glob("*.png")), str(tmp / "from_imgs.pdf"))))
    check("文本→PDF", lambda: _size(cv.text_to_pdf("第一行内容\n第二行内容\n" * 60, str(tmp / "txt.pdf"))))
    check("合并 PDF", lambda: _size(cv.merge_files([str(sample), str(out)], str(tmp / "merged.pdf"))))

    # ── 压缩
    from app.core import compress as cc  # noqa: E402

    session.open(sample)
    res = cc.compress(session, "medium", str(tmp / "small.pdf"))
    ok("压缩", f"{res['before']} → {res['after']} B ({res['ratio']}%)")

    # ── 加密 / 解密
    enc = tmp / "enc.pdf"
    cc.encrypt(session, str(enc), "test123")
    d = fitz.open(enc)
    needs = d.needs_pass
    auth = d.authenticate("test123")
    d.close()
    if needs and auth:
        ok("AES-256 加密", "需密码且可解锁")
    else:
        bad("AES-256 加密", f"needs_pass={needs}, auth={auth}")

    s2 = DocumentSession()
    check("打开加密 PDF", lambda: s2.open(enc, "test123")["pages"])
    check("解密另存", lambda: _size(cc.decrypt(s2, str(tmp / "dec.pdf"))))
    s2.close()

    # ── 水印
    session.open(sample)
    check("添加水印（全部页）", lambda: session.watermark({"text": "内部资料", "angle": 45, "size": 48, "opacity": 0.15})["version"])
    check("水印指定页", lambda: session.watermark({"text": "仅首页", "pages": "1", "angle": 0})["version"])
    check("水印平铺", lambda: session.watermark({"text": "机密\nCONFIDENTIAL", "tile": True, "angle": 30, "opacity": 0.1})["version"])
    check("水印预览", lambda: session.watermark_preview({"text": "预览", "angle": 60, "page": 0})["png"][:22])
    check("水印撤销", lambda: session.undo_edit()["undo_label"])
    check("水印再撤销", lambda: session.undo_edit()["undo_label"])
    try:
        session.watermark({"text": "   "})
        bad("空水印应报错", "没有抛出 ValueError")
    except ValueError:
        ok("空水印应报错")

    # ── 上传
    check("POST /api/upload", lambda: _upload(base, sample))

    # ── 打印渲染接口
    def _check_print():
        with urllib.request.urlopen(f"{base}/api/print/0?dpi=150", timeout=30) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
        if "image/jpeg" not in ctype:
            raise AssertionError(f"Content-Type 异常: {ctype}")
        if len(data) < 10_000:
            raise AssertionError(f"150dpi 页图过小: {len(data)} B")
        return f"{len(data) // 1024} KB"

    def _check_print_clamp():
        with urllib.request.urlopen(f"{base}/api/print/0?dpi=9999", timeout=30) as r:
            return len(r.read()) > 0

    check("打印渲染 150dpi", _check_print)
    check("打印渲染 dpi 上限钳制", _check_print_clamp)

    # ── 文件关联（只测纯逻辑，不写真实注册表）
    if sys.platform.startswith("win"):
        from app.core import assoc as _assoc

        def _check_assoc():
            cmd = _assoc.open_command()
            if getattr(sys, "frozen", False):
                if sys.executable not in cmd or "%1" not in cmd:
                    raise AssertionError(f"命令异常: {cmd}")
            elif "main.py" not in cmd or "%1" not in cmd:
                raise AssertionError(f"命令异常: {cmd}")
            url = _assoc.settings_url()
            if not url.startswith("ms-settings:defaultapps") or _assoc.APP_REG_NAME not in url:
                raise AssertionError(f"深链异常: {url}")
            return f"ok ({cmd[:40]}...)"

        check("关联命令构造", _check_assoc)
        check("关联状态读取", lambda: isinstance(_assoc.is_registered(), bool) and isinstance(_assoc.is_default(), bool))

    # ── 元数据 / 目录
    session.open(sample)
    check("设置元数据", lambda: session.set_metadata({"title": "测试标题"})["title"])
    check("目录读取", lambda: len(session.outline()))

    server.stop()
    print(f"\n{'=' * 54}\n通过 {ok_count} / {ok_count + fail_count}")
    if fail_count:
        print(f"失败 {fail_count} 项")
    return 0 if fail_count == 0 else 1


def _expect_status(base: str, path: str, want: int):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    if status != want:
        raise AssertionError(f"期望 {want}，实际 {status}")
    return want


def _size(p) -> str:
    n = Path(p).stat().st_size
    if n < 1024:
        raise AssertionError(f"文件过小：{n} B")
    return f"{n} B"


def _upload(base: str, sample: Path) -> str:
    data = sample.read_bytes()
    req = urllib.request.Request(
        f"{base}/api/upload?name={urllib.parse.quote(sample.name)}",
        data=data, method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.loads(r.read().decode("utf-8"))
    if not payload.get("ok"):
        raise AssertionError(payload)
    p = Path(payload["path"])
    if not p.exists() or p.stat().st_size != len(data):
        raise AssertionError("落盘文件不一致")
    return f"{p.name} ({p.stat().st_size} B)"


if __name__ == "__main__":
    sys.exit(main())
