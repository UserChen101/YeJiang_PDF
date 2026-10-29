/* ============================================================
   页匠 PageSmith — 前端主控
   ============================================================ */

import { Viewer } from './viewer.js';
import {
  openModal, closeModal, toast, toastErr,
  askText, askPassword, confirmDialog, choiceDialog,
  escapeHtml, escapeAttr, fmtBytes,
} from './dialog.js';

/* ───────────────────────────── 基础工具 ───────────────────────────── */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* 把运行时错误回传宿主终端，避免白屏时无从排查 */
function report(level, msg, extra = {}) {
  try {
    fetch('api/log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level, msg, ...extra }),
    }).catch(() => {});
  } catch { /* 忽略上报失败 */ }
}
window.addEventListener('error', (e) => {
  report('error', e.message, { src: e.filename, line: e.lineno, stack: e.error?.stack });
});
window.addEventListener('unhandledrejection', (e) => {
  report('reject', String(e.reason?.message || e.reason), { stack: e.reason?.stack });
});

const COLORS = {
  highlight: [1, 0.86, 0.32],
  underline: [0.23, 0.51, 0.96],
  strikeout: [0.86, 0.24, 0.24],
  note: [0.96, 0.62, 0.04],
};
const KIND_LABEL = { highlight: '高亮', underline: '下划线', strikeout: '删除线', note: '批注' };
const KIND_BG = {
  highlight: 'background:#fef3c7;color:#92400e',
  underline: 'background:#dbeafe;color:#1e40af',
  strikeout: 'background:#fee2e2;color:#991b1b',
  note: 'background:#ffedd5;color:#9a3412',
};

const state = {
  meta: null,
  annotations: [],
  selected: new Set(),
  tool: 'select',
  page: 0,
  scale: 1,
  pendingSel: null,
  search: { kw: '', pages: [], cursor: -1, done: false },
};

let viewer = null;
let loadingDepth = 0;

/* ───────────────────────────── 后端桥接 ───────────────────────────── */

let pythonReady = null;

function waitBackend() {
  if (pythonReady) return pythonReady;
  pythonReady = new Promise((resolve, reject) => {
    if (window.pywebview?.api) return resolve();
    const timer = setTimeout(() => reject(new Error('本地引擎没有响应，请重启应用')), 20000);
    window.addEventListener('pywebviewready', () => { clearTimeout(timer); resolve(); }, { once: true });
  });
  return pythonReady;
}

async function call(method, ...args) {
  await waitBackend();
  const fn = window.pywebview?.api?.[method];
  if (typeof fn !== 'function') throw new Error(`接口不可用：${method}`);
  const res = await fn(...args);
  if (!res || res.ok !== true) throw new Error(res?.error || '操作失败');
  return res.data;
}

/* ───────────────────────────── 状态提示 ───────────────────────────── */

function loading(on, text = '正在处理…') {
  const el = $('#loading');
  loadingDepth = Math.max(0, loadingDepth + (on ? 1 : -1));
  if (on) $('#loading-text').textContent = text;
  el.hidden = loadingDepth === 0;
}

function setHint(text) { $('#st-hint').textContent = text || ''; }

function markDirty(dirty) {
  $('#st-dirty').hidden = !dirty;
}

/* ───────────────────────────── 文档加载 ───────────────────────────── */

async function renderDoc(meta, { keepPage = true } = {}) {
  const prevPage = keepPage ? state.page : 0;
  state.meta = meta;
  state.annotations = meta.annotations || [];
  state.selected.clear();
  state.search = { kw: '', pages: [], cursor: -1, done: false };
  $('#searchbox').hidden = true;

  $('#welcome').hidden = true;
  $('#pages').hidden = false;

  // 文档变了（保存/撤销/页面操作），旧片段坐标全部作废
  edit.spans.clear();
  cancelInlineEdit();

  loading(true, '正在渲染文档…');
  try {
    await viewer.load(`doc.pdf?t=${encodeURIComponent(meta.token)}`, {
      annotations: state.annotations,
      restorePage: keyedPage(meta, prevPage),
    });
  } finally {
    loading(false);
  }

  buildThumbs();
  buildOutline(meta.outline || []);
  renderAnnotList();
  syncChrome();
  markDirty(!!meta.dirty);
}

function keyedPage(meta, fallback) {
  if (meta.restore_page && !fallback) return meta.restore_page;
  return Math.min(fallback ?? 0, (meta.pages || 1) - 1);
}

function syncChrome() {
  const has = !!state.meta && !state.meta.empty;
  $$('[data-act]').forEach((btn) => {
    const act = btn.dataset.act;
    if (['open', 'merge', 'images-to-pdf', 'docx-to-pdf', 'more', 'about', 'set-default-pdf', 'unset-default-pdf'].includes(act)) return;
    if (['zoom-in', 'zoom-out', 'fit-width', 'fit-page'].includes(act)) { btn.disabled = !has; return; }
    btn.disabled = !has;
  });

  // 菜单触发器「页面 / 转换 / 优化」用的是 data-menu，不是 data-act。
  // 上面的循环挑不到它们，于是会一直停在 HTML 里写死的 disabled 上 ——
  // 菜单内容再全也点不开。这里必须单独同步一次。
  $$('[data-menu]').forEach((btn) => {
    if (btn.dataset.menu === 'more') return;   // 「更多」里是另存为 / 关于，任何时候都能用
    btn.disabled = !has;
  });

  const meta = state.meta || {};
  $('#st-file').textContent = meta.path || '未打开文件';
  $('#st-size').textContent = meta.pages ? `${meta.pages} 页 · ${fmtBytes(meta.size)}` : '—';
  $('#st-page').textContent = meta.pages ? `${state.page + 1} / ${meta.pages}` : '— / —';
  $('#zoom-label').textContent = `${Math.round(state.scale * 100)}%`;
  $('#annot-count').textContent = String(state.annotations.length);
  document.title = meta.name ? `${meta.name} — 页匠` : '页匠 PageSmith';
}

/* ───────────────────────────── 缩略图 ───────────────────────────── */

function buildThumbs() {
  const box = $('#thumbs');
  box.replaceChildren();
  const n = state.meta?.pages || 0;
  const token = state.meta?.token || '';
  const frag = document.createDocumentFragment();

  for (let i = 0; i < n; i++) {
    const d = document.createElement('div');
    d.className = 'thumb';
    d.dataset.page = String(i);
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.src = `api/thumb/${i}?w=164&t=${encodeURIComponent(token)}`;
    img.alt = `第 ${i + 1} 页`;
    const mark = document.createElement('div');
    mark.className = 'sel-mark';
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = String(i + 1);
    d.append(mark, img, num);
    frag.appendChild(d);
  }
  box.appendChild(frag);
  refreshThumbSelection();
}

function refreshThumbSelection() {
  $$('.thumb').forEach((t) => {
    const i = Number(t.dataset.page);
    t.classList.toggle('selected', state.selected.has(i));
    t.classList.toggle('current', i === state.page);
  });
}

/* ───────────────────────────── 目录 ───────────────────────────── */

function buildOutline(nodes) {
  const box = $('#outline');
  box.replaceChildren();
  if (!nodes || !nodes.length) {
    box.innerHTML = '<p class="empty-hint">此文档没有书签目录</p>';
    return;
  }
  const walk = (list, depth) => {
    for (const node of list) {
      const btn = document.createElement('button');
      btn.className = 'outline-item';
      btn.style.paddingLeft = `${8 + depth * 13}px`;
      btn.innerHTML = `${escapeHtml(node.title)}<span class="pg">${node.page + 1}</span>`;
      btn.addEventListener('click', () => viewer.gotoPage(node.page));
      box.appendChild(btn);
      if (node.children?.length) walk(node.children, depth + 1);
    }
  };
  walk(nodes, 0);
}

/* ───────────────────────────── 批注列表 ───────────────────────────── */

function renderAnnotList() {
  const box = $('#annot-list');
  $('#annot-count').textContent = String(state.annotations.length);

  if (!state.annotations.length) {
    box.innerHTML = '<p class="empty-hint">还没有批注<br>选中文字即可高亮或加批注</p>';
    return;
  }

  box.replaceChildren();
  const sorted = [...state.annotations].sort((a, b) => a.page - b.page);
  for (const a of sorted) {
    const card = document.createElement('div');
    card.className = 'annot-card';
    card.style.position = 'relative';
    const preview = a.content || (a.kind === 'note' ? '（空批注）' : '');
    card.innerHTML = `
      <div class="head">
        <span class="kind" style="${KIND_BG[a.kind] || KIND_BG.highlight}">${KIND_LABEL[a.kind] || a.kind}</span>
        <span class="pgno">第 ${a.page + 1} 页</span>
      </div>
      ${preview ? `<div class="txt">${escapeHtml(preview)}</div>` : ''}
      <button class="del" title="删除" style="right:6px;top:6px">
        <svg viewBox="0 0 24 24" style="width:13px;height:13px"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>`;

    card.addEventListener('click', (e) => {
      if (e.target.closest('.del')) return;
      viewer.gotoPage(a.page);
      flashAnnot(a);
    });

    card.querySelector('.del').addEventListener('click', async (e) => {
      e.stopPropagation();
      await removeAnnot(a.id);
    });

    box.appendChild(card);
  }
}

function flashAnnot(a) {
  const pin = document.querySelector(`.annot-mark[data-aid="${a.id}"]`);
  if (!pin) return;
  pin.style.transition = 'box-shadow .2s';
  pin.style.boxShadow = '0 0 0 3px rgba(37,99,235,.55)';
  setTimeout(() => { pin.style.boxShadow = ''; }, 900);
}

async function removeAnnot(id) {
  try {
    await call('remove_annotation', id);
    state.annotations = state.annotations.filter((x) => x.id !== id);
    viewer.setAnnotations(state.annotations);
    renderAnnotList();
    markDirty(true);
  } catch (err) { toastErr(err); }
}

/* ───────────────────────────── 选区处理 ───────────────────────────── */

let selTimer = null;

function onSelectionSettled() {
  clearTimeout(selTimer);
  selTimer = setTimeout(inspectSelection, 15);
}

function inspectSelection() {
  const bar = $('#float-toolbar');
  if (state.tool !== 'select') { bar.classList.remove('show'); return; }

  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
    bar.classList.remove('show');
    state.pendingSel = null;
    return;
  }

  const rects = viewer.selectionToRects();
  if (!rects || !rects.length) { bar.classList.remove('show'); state.pendingSel = null; return; }

  state.pendingSel = rects;
  const range = sel.getRangeAt(0);
  const box = range.getBoundingClientRect();
  const wrap = $('#viewer-wrap').getBoundingClientRect();

  bar.classList.add('show');
  const bw = bar.offsetWidth || 300;
  let left = box.left - wrap.left + box.width / 2 - bw / 2;
  left = Math.max(8, Math.min(left, wrap.width - bw - 8));
  let top = box.top - wrap.top - bar.offsetHeight - 9;
  if (top < 6) top = box.bottom - wrap.top + 9;
  bar.style.left = `${left}px`;
  bar.style.top = `${top}px`;
}

async function applyFormat(kind) {
  const sels = state.pendingSel;
  if (!sels || !sels.length) return;

  if (kind === 'copy') {
    const text = window.getSelection()?.toString() || '';
    await copyText(text);
    toast(`已复制 ${text.length} 个字符`);
    return;
  }

  let content = '';
  if (kind === 'note') {
    content = await askText({
      title: '添加批注',
      label: '批注内容',
      placeholder: '写下你的想法…',
      confirmLabel: '添加',
      multiline: true,
    });
    if (!content) return;
  }

  try {
    const created = [];
    for (const s of sels) {
      const payload = {
        kind,
        page: s.page,
        rects: s.rects,
        color: COLORS[kind],
        content,
        opacity: kind === 'highlight' ? 0.42 : 1,
      };
      const a = await call('add_annotation', payload);
      created.push(a);
    }
    state.annotations.push(...created);
    viewer.setAnnotations(state.annotations);
    renderAnnotList();
    markDirty(true);
    window.getSelection()?.removeAllRanges();
    $('#float-toolbar').classList.remove('show');
    toast(`已添加 ${created.length} 处${KIND_LABEL[kind]}`);
  } catch (err) { toastErr(err); }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

/* ───────────────────────────── 搜索 ───────────────────────────── */

async function runSearch(kw) {
  state.search = { kw, pages: [], cursor: -1, done: false };
  const countEl = $('#search-count');
  if (!kw) { countEl.textContent = ''; viewer.clearSearchHits(); return; }

  countEl.textContent = '搜索中';
  const pages = [];
  try {
    for await (const hit of viewer.searchAll(kw)) pages.push(hit.page);
  } catch { /* 忽略中断 */ }
  state.search.pages = pages;
  state.search.done = true;
  countEl.textContent = pages.length ? `1 / ${pages.length}` : '无结果';
  await viewer.paintSearchHits(pages, kw);
  if (pages.length) {
    state.search.cursor = 0;
    viewer.gotoPage(pages[0]);
  }
}

function stepSearch(dir) {
  const { pages } = state.search;
  if (!pages.length) return;
  state.search.cursor = (state.search.cursor + dir + pages.length) % pages.length;
  $('#search-count').textContent = `${state.search.cursor + 1} / ${pages.length}`;
  viewer.gotoPage(pages[state.search.cursor]);
}

/* ───────────────────────────── 页面操作 ───────────────────────────── */

async function reloadAfter(action, meta) {
  if (!meta) return;
  markDirty(true);
  if (action === 'delete') state.page = Math.min(state.page, meta.pages - 1);
  await renderDoc(meta, { keepPage: true });
  setHint('页面已更新');
  setTimeout(() => setHint(''), 2200);
}

async function doRotate(angle = 90, pages = null) {
  if (!state.meta) return;
  const targets = pages || (state.selected.size ? [...state.selected] : [state.page]);
  loading(true, '正在旋转…');
  try {
    const meta = await call('rotate', targets, angle);
    await reloadAfter('rotate', meta);
    toast(`已旋转 ${targets.length} 页`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doDeletePages() {
  const targets = state.selected.size ? [...state.selected] : [state.page];
  if (!state.meta) return;
  if (targets.length >= state.meta.pages) {
    toastErr('不能删除全部页面');
    return;
  }
  const ok = await confirmDialog({
    title: '删除页面',
    message: `确定删除第 ${targets.sort((a, b) => a - b).map((i) => i + 1).join('、')} 页吗？此操作在保存前不会影响原文件。`,
    confirmLabel: '删除',
    danger: true,
  });
  if (!ok) return;
  loading(true, '正在删除…');
  try {
    const meta = await call('delete_pages', targets);
    state.selected.clear();
    await reloadAfter('delete', meta);
    toast(`已删除 ${targets.length} 页`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doExtractPages() {
  const targets = state.selected.size ? [...state.selected] : [state.page];
  try {
    const r = await call('extract_dialog', targets);
    if (r?.cancelled) return;
    toast(`已提取到 ${r.path}`);
  } catch (err) { toastErr(err); }
}

async function doMovePage(src, dst) {
  if (src === dst) return;
  loading(true, '正在调整顺序…');
  try {
    const meta = await call('move_page', src, dst);
    state.page = dst;
    await renderDoc(meta, { keepPage: true });
    toast(`已移动到第 ${dst + 1} 位`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

/* ───────────────────────────── 保存 ───────────────────────────── */

async function doSave() {
  if (!state.meta) return;
  loading(true, '正在保存…');
  try {
    let r = await call('save');
    if (r?.need_path) {
      loading(false);
      return doSaveAs();
    }
    await renderDoc(r.meta, { keepPage: true });
    toast('已保存到原文件');
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doSaveAs() {
  if (!state.meta) return;
  loading(true, '正在准备…');
  loading(false);
  try {
    const r = await call('save_as_dialog', '', '', '');
    if (r?.cancelled) return;
    toast(`已保存到 ${r.path}`);
    await renderDoc(r.meta, { keepPage: true });
  } catch (err) { toastErr(err); }
}

/* ───────────────────────────── 功能对话框 ───────────────────────────── */

async function doCompress() {
  const level = await choiceDialog({
    title: '压缩文件体积',
    message: '图片重采样会降低清晰度，文字不会受影响。建议先试「标准」。',
    confirmLabel: '开始压缩',
    options: [
      { value: 'light', title: '轻度', desc: '只清理文件结构冗余，画质完全无损' },
      { value: 'medium', title: '标准（推荐）', desc: '图片最大 1600px、JPEG 质量 78，通常减 40%~70%' },
      { value: 'strong', title: '强力', desc: '图片最大 1000px、质量 58，适合网页发布' },
      { value: 'extreme', title: '极限', desc: '转为灰度、760px，仅适合邮件传输' },
    ],
  });
  if (!level) return;

  const keep = await confirmDialog({
    title: '保存方式',
    message: '点「保留原文件」会另存为 xxx_压缩.pdf；点「覆盖原文件」会直接替换。',
    confirmLabel: '保留原文件',
  });

  loading(true, '正在压缩…');
  try {
    const r = keep ? await call('compress', level, true) : await call('compress_as_dialog', level);
    if (r?.cancelled) return;
    if (r?.note) { toast(r.note, 'info'); return; }
    const pct = r.ratio > 0 ? `，减少 ${r.ratio}%` : '';
    toast(`${fmtBytes(r.before)} → ${fmtBytes(r.after)}${pct}`);
    if (keep && r.path) await refreshAfterExternalChange();
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function refreshAfterExternalChange() {
  try {
    const st = await call('get_state');
    if (st && !st.empty) await renderDoc(st, { keepPage: true });
  } catch { /* 忽略 */ }
}

async function doOcr() {
  const has = state.meta?.ocr;
  if (!has) {
    toast('OCR 组件未安装，无法识别扫描件', 'err', 4200);
    return;
  }
  const range = await askText({
    title: 'OCR 文字识别',
    label: '识别范围',
    value: '',
    placeholder: '留空 = 全部页面；也可填 1-5,8',
    hint: '识别结果会以「不可见文字」写入 PDF，之后就能搜索和复制，但不会改变版面外观。',
    confirmLabel: '开始识别',
  });
  if (range === null) return;

  loading(true, '正在识别文字，页数多时需要几分钟…');
  try {
    const r = await call('ocr', range || null, 300);
    toast(`识别完成：${r.blocks} 个文本块，写入 ${r.written} 处`, 'ok', 4000);
    await refreshAfterExternalChange();
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

/* ───────────────────────────── 水印 ───────────────────────────── */

async function doWatermark() {
  if (!state.meta) return;

  const ANGLES = [0, 45, 90, 135, 180, 225, 270, 315];
  let payload = null;   // 点「添加水印」时捕获
  let timer = 0;        // 预览防抖
  let seq = 0;          // 丢弃过期预览结果
  let inflight = 0;

  // 字体列表在弹窗打开前就取好，选项直接烘进 HTML，避免异步填充闪空
  let fonts = [];
  try { fonts = await call('fonts'); } catch { fonts = []; }

  function readWm(root) {
    const range = root.querySelector('#wm-range .seg-btn.active')?.dataset.range || 'all';
    const pages = range === 'all' ? ''
      : range === 'current' ? String(state.page + 1)
      : root.querySelector('#wm-pages').value.trim();
    return {
      text: root.querySelector('#wm-text').value,
      font: root.querySelector('#wm-font').value || '',
      size: Number(root.querySelector('#wm-size').value) || 48,
      color: root.querySelector('#wm-color').value,
      opacity: Math.max(2, Math.min(100, Number(root.querySelector('#wm-op').value) || 18)) / 100,
      angle: Number(root.querySelector('#wm-angle').value) || 0,
      tile: root.querySelector('#wm-mode .seg-btn.active')?.dataset.mode === 'tile',
      pages,
    };
  }

  return openModal({
    title: '添加水印',
    width: 700,
    body: `
      <div class="wm-grid">
        <div>
          <div class="field"><label>水印文字（可换行）</label>
            <textarea id="wm-text" rows="2">内部资料</textarea></div>

          <div class="wm-field-row">
            <div class="field"><label>字体</label><select id="wm-font">${
              fonts.map((f) => `<option value="${escapeAttr(f.id)}">${escapeHtml(f.label)}</option>`).join('')
            }</select></div>
            <div class="field"><label>字号（pt）</label>
              <input type="number" id="wm-size" min="4" max="400" step="1" value="48"></div>
          </div>

          <div class="field"><label>方向（0–360° 逆时针）</label>
            <div class="wm-angle-row">
              <input type="range" id="wm-angle" min="0" max="360" step="1" value="45">
              <span class="wm-angle-val" id="wm-angle-val">45°</span>
            </div>
            <div class="wm-presets" id="wm-presets">
              ${ANGLES.map((a) => `<button type="button" data-angle="${a}"${a === 45 ? ' class="active"' : ''}>${a}°</button>`).join('')}
            </div></div>

          <div class="wm-mini-row">
            <div class="field"><label>颜色</label><input type="color" id="wm-color" value="#8a8a8a"></div>
            <div class="field"><label>不透明度</label>
              <div class="wm-opacity-row">
                <input type="range" id="wm-op" min="2" max="100" step="1" value="18">
                <span class="wm-opacity-val" id="wm-op-val">18%</span>
              </div></div>
          </div>

          <div class="wm-field-row">
            <div class="field"><label>铺设方式</label>
              <div class="seg" id="wm-mode">
                <button class="seg-btn active" data-mode="single" type="button">单个居中</button>
                <button class="seg-btn" data-mode="tile" type="button">平铺整页</button>
              </div></div>
            <div class="field"><label>应用范围</label>
              <div class="seg" id="wm-range">
                <button class="seg-btn active" data-range="all" type="button">全部页</button>
                <button class="seg-btn" data-range="current" type="button">当前页</button>
                <button class="seg-btn" data-range="custom" type="button">指定页</button>
              </div></div>
          </div>

          <input type="text" class="wm-plain" id="wm-pages" placeholder="页码范围，例如 1-3,5,8-" hidden>
        </div>

        <div class="wm-side">
          <div class="wm-preview">
            <img id="wm-img" alt="水印预览" hidden>
            <span class="wm-empty" id="wm-empty">正在生成预览…</span>
            <div class="wm-busy" id="wm-busy" hidden>预览中…</div>
          </div>
          <p class="hint">预览即最终效果。水印是真实文字（可搜索、可复制），不是图片。</p>
        </div>
      </div>`,
    buttons: [
      { label: '取消', value: null },
      {
        label: '添加水印', primary: true, value: 'apply',
        onClick: ({ body }) => {
          payload = readWm(body);
          if (!payload.text.trim()) {
            $('#wm-text', body).focus();
            toastErr('水印文字不能为空');
            return false;
          }
        },
      },
    ],
    onMount: (body) => {
      const $b = (sel) => $(sel, body);

      // 分段按钮通用切换
      for (const sel of ['#wm-mode', '#wm-range']) {
        $b(sel).addEventListener('click', (e) => {
          const btn = e.target.closest('.seg-btn');
          if (!btn) return;
          $b(sel).querySelectorAll('.seg-btn').forEach((x) => x.classList.remove('active'));
          btn.classList.add('active');
          if (sel === '#wm-range') $b('#wm-pages').hidden = btn.dataset.range !== 'custom';
          schedule();
        });
      }

      $b('#wm-angle').addEventListener('input', () => {
        $b('#wm-angle-val').textContent = `${$b('#wm-angle').value}°`;
        $b('#wm-presets').querySelectorAll('button').forEach((b) =>
          b.classList.toggle('active', Number(b.dataset.angle) === Number($b('#wm-angle').value)));
        schedule();
      });
      $b('#wm-presets').addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-angle]');
        if (!btn) return;
        $b('#wm-angle').value = btn.dataset.angle;
        $b('#wm-angle-val').textContent = `${btn.dataset.angle}°`;
        $b('#wm-presets').querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === btn));
        schedule();
      });
      $b('#wm-op').addEventListener('input', () => {
        $b('#wm-op-val').textContent = `${$b('#wm-op').value}%`;
        schedule();
      });
      for (const sel of ['#wm-text', '#wm-size', '#wm-color', '#wm-font', '#wm-pages']) {
        $b(sel).addEventListener('input', schedule);
      }

      async function runPreview() {
        const my = ++seq;
        inflight++;
        $b('#wm-busy').hidden = false;
        try {
          const r = await call('watermark_preview', { ...readWm(body), page: state.page || 0, width: 440 });
          if (my === seq) {
            $b('#wm-img').src = r.png;
            $b('#wm-img').hidden = false;
            $b('#wm-empty').hidden = true;
          }
        } catch { /* 预览失败不打扰用户，表单还能继续用 */ }
        finally {
          inflight--;
          if (!inflight) $b('#wm-busy').hidden = true;
        }
      }

      function schedule() {
        clearTimeout(timer);
        timer = setTimeout(runPreview, 260);
      }

      schedule();
    },
    }).then(async (v) => {
    if (v !== 'apply' || !payload) return;
    loading(true, '正在添加水印…');
    try {
      const meta = await call('watermark', payload);
      await renderDoc(meta, { keepPage: true });
      toast('水印已添加 · Ctrl+Z 可撤销');
    } catch (err) { toastErr(err); }
    finally { loading(false); }
  });
}

async function doUndo() {
  try {
    const meta = await call('undo_edit');
    await renderDoc(meta, { keepPage: true });
    toast(`已撤销：${meta.undo_label || '上一步操作'}`);
  } catch (err) { toastErr(err); }
}

async function doEncrypt() {
  const res = await collectEncrypt();
  if (!res) return;
  if (!res.user) { toastErr('打开密码不能为空'); return; }
  loading(true, '正在加密…');
  try {
    const r = await call('encrypt_dialog', res.user, res.owner, res.print, res.copy);
    if (r?.cancelled) return;
    toast(`已加密保存到 ${r.path}`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

function collectEncrypt() {
  return openModal({
    title: '设置密码',
    body: `
      <div class="field"><label>打开密码（必填）</label>
        <input type="password" id="enc-user" placeholder="打开文件时需要输入"></div>
      <div class="field"><label>权限密码（选填）</label>
        <input type="password" id="enc-owner" placeholder="留空则与打开密码相同">
        <p class="hint">采用 AES-256 加密，是当前 PDF 标准中最安全的一档。</p></div>
      <label class="check"><input type="checkbox" id="enc-print" checked> 允许打印</label>
      <label class="check"><input type="checkbox" id="enc-copy" checked> 允许复制文字</label>`,
    buttons: [
      { label: '取消', value: null },
      { label: '加密并另存', primary: true, value: 'go' },
    ],
  }).then((v) => {
    if (v !== 'go') return null;
    const user = $('#enc-user')?.value || '';
    return {
      user,
      owner: $('#enc-owner')?.value || '',
      print: $('#enc-print')?.checked ?? true,
      copy: $('#enc-copy')?.checked ?? true,
    };
  });
}

async function doDecrypt() {
  loading(true, '正在移除密码…');
  try {
    const r = await call('decrypt_dialog');
    if (r?.cancelled) return;
    toast(`已生成无密码版本：${r.path}`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doProps() {
  const m = state.meta || {};
  const go = await openModal({
    title: '文档属性',
    width: 500,
    body: `
      <div class="kv" style="margin-bottom:18px">
        <dt>文件名</dt><dd>${escapeHtml(m.name || '—')}</dd>
        <dt>路径</dt><dd>${escapeHtml(m.path || '—')}</dd>
        <dt>页数</dt><dd>${m.pages || 0}</dd>
        <dt>体积</dt><dd>${fmtBytes(m.size)}</dd>
        <dt>加密</dt><dd>${m.encrypted ? '是' : '否'}</dd>
      </div>
      <div class="field"><label>标题</label><input type="text" id="md-title" value="${escapeHtml(m.title || '')}"></div>
      <div class="field"><label>作者</label><input type="text" id="md-author" value="${escapeHtml(m.author || '')}"></div>
      <div class="field"><label>主题</label><input type="text" id="md-subject" value="${escapeHtml(m.subject || '')}"></div>
      <div class="field"><label>生成工具</label><input type="text" value="${escapeHtml(m.producer || '')}" disabled></div>`,
    buttons: [
      { label: '关闭', value: null },
      { label: '保存属性', primary: true, value: 'save' },
    ],
  });
  if (go !== 'save') return;
  try {
    const meta = await call('set_metadata', {
      title: $('#md-title')?.value || '',
      author: $('#md-author')?.value || '',
      subject: $('#md-subject')?.value || '',
    });
    state.meta = { ...state.meta, ...meta };
    markDirty(true);
    toast('属性已更新（保存后写入文件）');
  } catch (err) { toastErr(err); }
}

async function doConvert(kind) {
  if (!state.meta) return;
  const names = { docx: 'Word', xlsx: 'Excel', images: '图片', html: '网页', txt: '纯文本' };
  loading(true, `正在转换为 ${names[kind] || kind}…`);
  try {
    const opts = kind === 'images' ? [kind, null, 200, 'png', 88] : [kind, null, 150, 'png', 88];
    const r = await call('convert_dialog', ...opts);
    if (r?.cancelled) return;
    if (r.count) toast(`已导出 ${r.count} 个文件到 ${r.path || '所选目录'}`, 'ok', 3800);
    else toast(`已导出到 ${r.path}`, 'ok', 3800);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doInsertPdf() {
  loading(true, '正在插入…');
  try {
    const meta = await call('insert_pdf_dialog');
    if (meta?.cancelled) return;
    await renderDoc(meta, { keepPage: true });
    toast('已插入 PDF');
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doInsertImages() {
  loading(true, '正在插入图片…');
  try {
    const meta = await call('insert_images_dialog');
    if (meta?.cancelled) return;
    await renderDoc(meta, { keepPage: true });
    toast('已插入图片');
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doSplit() {
  const every = await askText({
    title: '按页拆分',
    label: '每个文件包含多少页',
    value: '1',
    hint: '例如填 10，则每 10 页生成一个 PDF，输出到你所选的文件夹。',
    confirmLabel: '选择输出文件夹',
  });
  if (!every) return;
  const n = parseInt(every, 10);
  if (!n || n < 1) { toastErr('请输入大于 0 的整数'); return; }
  loading(true, '正在拆分…');
  try {
    const r = await call('split_dialog', n);
    if (r?.cancelled) return;
    toast(`已拆分为 ${r.count} 个文件`, 'ok', 3800);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function doMerge() {
  loading(true, '正在合并…');
  try {
    const meta = await call('merge_dialog');
    if (meta?.cancelled) return;
    await renderDoc(meta, { keepPage: false });
    toast('合并完成');
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

async function exportAnnotations() {
  if (!state.annotations.length) { toast('还没有批注可以导出', 'info'); return; }
  const lines = ['页码\t类型\t内容'];
  for (const a of [...state.annotations].sort((x, y) => x.page - y.page)) {
    lines.push(`${a.page + 1}\t${KIND_LABEL[a.kind] || a.kind}\t${(a.content || '').replace(/\s+/g, ' ')}`);
  }
  try {
    const r = await call('save_text_dialog', lines.join('\n'), '批注清单.txt');
    if (r?.cancelled) return;
    toast(`批注清单已保存到 ${r.path}`);
  } catch (err) { toastErr(err); }
}

/* ───────────────────────────── 打开文件 ───────────────────────────── */

async function doOpen() {
  loading(true, '正在打开…');
  try {
    let r = await call('open_dialog');
    loading(false);
    if (r?.cancelled) return;
    if (r?.need_password) {
      const pw = await askPassword({ title: '需要密码', label: `「${r.name}」已加密，请输入打开密码` });
      if (!pw) return;
      loading(true, '正在解锁…');
      try {
        r = await call('unlock', r.path, pw);
      } finally { loading(false); }
    }
    await renderDoc(r, { keepPage: false });
    setHint(`已打开 ${r.name}`);
  } catch (err) {
    loading(false);
    toastErr(err);
  } finally {
    while (loadingDepth > 0) loading(false);
  }
}

async function openRecent(path) {
  loading(true, '正在打开…');
  try {
    const r = await call('open_path', path, '');
    await renderDoc(r, { keepPage: false });
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

function renderRecent(list) {
  const box = $('#recent');
  box.replaceChildren();
  if (!list || !list.length) return;
  const h = document.createElement('h4');
  h.textContent = '最近打开';
  box.appendChild(h);
  for (const item of list.slice(0, 5)) {
    const b = document.createElement('button');
    b.className = 'recent-item';
    b.innerHTML = `<span class="nm">${escapeHtml(item.name)}</span><span class="mt">${item.pages} 页</span>`;
    b.title = item.path;
    b.addEventListener('click', () => openRecent(item.path));
    box.appendChild(b);
  }
}

/* ───────────────────────────── 工具栏动作分发 ───────────────────────────── */

/* ── 默认 PDF 打开工具 ── */

async function refreshDefaultPdfMenu() {
  // 「更多」菜单每次展开时同步两个按钮的显隐（二选一）
  try {
    const st = await call('pdf_assoc_status');
    const isDefault = !!st.is_default;
    const setBtn = $('#act-set-default-pdf');
    const unsetBtn = $('#act-unset-default-pdf');
    if (setBtn) setBtn.hidden = isDefault;
    if (unsetBtn) unsetBtn.hidden = !isDefault;
  } catch (e) { /* 状态获取失败时保持默认展示 */ }
}

async function doSetDefaultPdf() {
  loading(true, '正在设置默认打开方式…');
  try {
    const r = await call('set_default_pdf_app');
    if (r.is_default) {
      toast('页匠已是默认 PDF 打开工具');
      return;
    }
    if (r.applied) {
      toast('已设为默认 PDF 打开工具');
    } else {
      // Windows 哈希保护：注册已就位，最后一步请用户在系统设置里点一下
      openModal({
        title: '最后一步',
        body: `<p>已把页匠注册进系统「默认应用」列表。</p>
          <p>设置窗口已自动打开，请：<br>
          1. 在应用列表中找到「页匠 PageSmith」<br>
          2. 点击它，再点「.pdf」旁的「设为默认」</p>
          <p class="hint">只需设置一次，以后双击 PDF 都会用页匠打开。</p>`,
        buttons: [
          { label: '打开设置页', value: 'settings' },
          { label: '知道了', primary: true, value: true },
        ],
      }).then((v) => {
        if (v === 'settings') call('open_settings_page').catch(() => {});
      });
    }
    refreshDefaultPdfMenu();
  } catch (e) { toastErr(e); } finally { loading(false); }
}

async function doUnsetDefaultPdf() {
  try {
    await call('unset_default_pdf_app');
    toast('已取消页匠的默认 PDF 关联注册');
    refreshDefaultPdfMenu();
  } catch (e) { toastErr(e); }
}

/* ── 打印 ──
 * 不能直接 window.print()：那会把整个界面（连续滚动容器 + 侧栏 + 缩放）
 * 当网页打印，PDF 页边界和纸张边界对不上，全部错位。
 * 正确做法：把每页渲染成高清图，写进隐藏 iframe，一图一纸逐页打印。 */
async function doPrint() {
  if (!state.meta || state.meta.empty) { toast('请先打开一个 PDF'); return; }
  const pages = state.meta.pages || 0;
  if (!pages) return;
  const token = encodeURIComponent(state.meta.token || '');
  loading(true, `正在准备打印（0/${pages}）…`);
  let frame = null;
  try {
    frame = document.createElement('iframe');
    Object.assign(frame.style, {
      position: 'fixed', right: '0', bottom: '0',
      width: '0', height: '0', border: '0', visibility: 'hidden',
    });
    document.body.appendChild(frame);
    const pdoc = frame.contentDocument;
    pdoc.open();
    pdoc.write('<!DOCTYPE html><html><head><style>'
      + '@page { margin: 0; }'
      + 'html, body { margin: 0; padding: 0; }'
      + 'img { display: block; width: 100%; page-break-after: always; break-after: page; }'
      + 'img:last-child { page-break-after: auto; break-after: auto; }'
      + '</style></head><body></body></html>');
    pdoc.close();

    const urls = [];
    for (let i = 0; i < pages; i++) {
      loading(true, `正在准备打印（${i + 1}/${pages}）…`);
      const resp = await fetch(`api/print/${i}?dpi=150&t=${token}`);
      if (!resp.ok) throw new Error(`第 ${i + 1} 页渲染失败`);
      const url = URL.createObjectURL(await resp.blob());
      urls.push(url);
      const img = pdoc.createElement('img');
      await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error(`第 ${i + 1} 页加载失败`)); img.src = url; });
      pdoc.body.appendChild(img);
    }
    await new Promise((r) => setTimeout(r, 250)); // 等最后一页布局稳定
    loading(false);
    frame.contentWindow.focus();
    // 注意：print() 返回不代表打印成功 —— 用户点「取消」也一样返回，
    // 无法区分，所以这里不做任何"已发送"提示，以免误导。
    frame.contentWindow.print();
  } catch (e) {
    toastErr(e);
  } finally {
    loading(false);
    if (frame) setTimeout(() => frame.remove(), 0);
  }
}

const ACTIONS = {
  open: doOpen,
  save: doSave,
  'save-as': doSaveAs,
  'zoom-in': () => viewer.setScale(state.scale * 1.2),
  'zoom-out': () => viewer.setScale(state.scale / 1.2),
  'fit-width': () => viewer.fitWidth(),
  'fit-page': () => viewer.fitPage(),
  rotate: () => doRotate(90),
  'delete-pages': doDeletePages,
  'extract-pages': doExtractPages,
  reverse: async () => {
    loading(true, '正在反转…');
    try { await reloadAfter('reverse', await call('reverse')); toast('页面顺序已反转'); }
    catch (e) { toastErr(e); } finally { loading(false); }
  },
  'insert-pdf': doInsertPdf,
  'insert-images': doInsertImages,
  'blank-page': async () => {
    try { await reloadAfter('blank', await call('blank_page', null)); toast('已添加空白页'); }
    catch (e) { toastErr(e); }
  },
  split: doSplit,
  merge: doMerge,
  'to-docx': () => doConvert('docx'),
  'to-xlsx': () => doConvert('xlsx'),
  'to-images': () => doConvert('images'),
  'to-html': () => doConvert('html'),
  'to-txt': () => doConvert('txt'),
  'images-to-pdf': async () => {
    loading(true, '正在合成…');
    try {
      const meta = await call('files_to_pdf_dialog', 'img');
      if (meta?.cancelled) return;
      await renderDoc(meta, { keepPage: false });
      toast('已合成 PDF');
    } catch (e) { toastErr(e); } finally { loading(false); }
  },
  'docx-to-pdf': async () => {
    loading(true, '正在转换…');
    try {
      const meta = await call('files_to_pdf_dialog', 'doc');
      if (meta?.cancelled) return;
      await renderDoc(meta, { keepPage: false });
      toast('已转换为 PDF');
    } catch (e) { toastErr(e); } finally { loading(false); }
  },
  compress: doCompress,
  ocr: doOcr,
  watermark: doWatermark,
  encrypt: doEncrypt,
  decrypt: doDecrypt,
  props: doProps,
  'edit-text': () => toggleEdit(),
  search: () => toggleSearch(true),
  print: () => doPrint(),
  'export-annot': exportAnnotations,
  reveal: async () => { try { await call('reveal_in_explorer', ''); } catch (e) { toastErr(e); } },
  'open-external': async () => { try { await call('open_external', ''); } catch (e) { toastErr(e); } },
  'set-default-pdf': doSetDefaultPdf,
  'unset-default-pdf': doUnsetDefaultPdf,
  about: () => openModal({
    title: '关于 页匠',
    body: `<div class="kv">
        <dt>名称</dt><dd>页匠 PageSmith</dd>
        <dt>版本</dt><dd>1.0.3</dd>
        <dt>渲染引擎</dt><dd>pdf.js（Firefox 同源）</dd>
        <dt>文档引擎</dt><dd>PyMuPDF / MuPDF</dd>
        <dt>界面</dt><dd>WebView2 + 原生桌面壳</dd>
      </div>
      <p class="hint" style="margin-top:16px">所有 PDF 处理都在本机完成，文件不会离开这台电脑。</p>`,
    buttons: [{ label: '知道了', primary: true, value: true }],
  }),
};

async function runAction(act, el) {
  const fn = ACTIONS[act];
  if (!fn) return;
  try {
    const r = fn(el);
    if (r && typeof r.then === 'function') await r;
  } catch (err) { toastErr(err); }
}

/* ───────────────────────────── 文字编辑 ───────────────────────────── */

const FONT_CSS = {
  msyh: "'Microsoft YaHei', 'PingFang SC', sans-serif",
  simsun: "SimSun, 'Songti SC', serif",
  simhei: "SimHei, sans-serif",
  simkai: "KaiTi, 'Kaiti SC', serif",
  simfang: "FangSong, serif",
  deng: "DengXian, sans-serif",
  arial: "Arial, sans-serif",
  times: "'Times New Roman', serif",
  cour: "'Courier New', monospace",
};

const edit = {
  on: false,
  adding: false,
  fonts: [],
  font: 'msyh',
  size: 14,
  dir: 'h',
  color: '#111111',
  spans: new Map(),   // page -> 文本片段数组（pdf.js 坐标）
  active: null,       // 行内编辑器 { page, ta, kind, ... }
};

function editCssFont(fid) { return FONT_CSS[fid] || FONT_CSS.msyh; }

function rgbToHex(rgb) {
  return '#' + rgb.map((c) => Math.round(Math.min(1, Math.max(0, c)) * 255).toString(16).padStart(2, '0')).join('');
}

function guessFontId(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('yahei') || n.includes('雅黑')) return 'msyh';
  if (n.includes('simsun') || n.includes('song') || n.includes('宋')) return 'simsun';
  if (n.includes('simhei') || n.includes('hei') || n.includes('黑')) return 'simhei';
  if (n.includes('kai') || n.includes('楷')) return 'simkai';
  if (n.includes('fang') || n.includes('仿')) return 'simfang';
  if (n.includes('deng') || n.includes('等线')) return 'deng';
  if (n.includes('times')) return 'times';
  if (n.includes('cour')) return 'cour';
  if (n.includes('arial') || n.includes('helvetica')) return 'arial';
  return edit.fonts.some((f) => f.id === edit.font) ? edit.font : (edit.fonts[0]?.id || 'msyh');
}

async function toggleEdit(force) {
  edit.on = force ?? !edit.on;
  if (edit.on && !state.meta) { edit.on = false; return; }
  $('#viewer-wrap').classList.toggle('edit-mode', edit.on);
  $('#editbar').hidden = !edit.on;
  $('#btn-edit-text')?.classList.toggle('active', edit.on);

  if (edit.on) {
    cancelInlineEdit();
    if (!edit.fonts.length) {
      try {
        edit.fonts = await call('fonts') || [];
        const sel = $('#edit-font');
        sel.replaceChildren(...edit.fonts.map((f) => new Option(f.label, f.id)));
        if (!edit.fonts.some((f) => f.id === edit.font)) edit.font = edit.fonts[0]?.id || 'msyh';
        sel.value = edit.font;
      } catch (err) { toastErr(err); }
    }
    for (const p of viewer.pages) { if (p.vp) ensureEditSpans(p.index); }
    toast('点击文字直接修改；清空文字即删除；「添加文字」后点页面空白处新增', 'info', 4200);
  } else {
    edit.adding = false;
    $('#edit-add')?.classList.remove('active');
    $('#viewer-wrap').classList.remove('adding');
    cancelInlineEdit();
    for (const p of viewer.pages) p.editEl?.replaceChildren();
    edit.spans.clear();
  }
}

async function ensureEditSpans(pageIdx) {
  const p = viewer.pages[pageIdx];
  if (!p || !p.vp || !edit.on) return;
  if (!edit.spans.has(pageIdx)) {
    try { edit.spans.set(pageIdx, (await call('text_spans', pageIdx)) || []); }
    catch { edit.spans.set(pageIdx, []); }
  }
  renderEditSpans(pageIdx);
}

function renderEditSpans(pageIdx) {
  const p = viewer.pages[pageIdx];
  if (!p || !p.vp || !p.editEl) return;
  const spans = edit.spans.get(pageIdx) || [];
  const frag = document.createDocumentFragment();
  spans.forEach((sp, i) => {
    const [x0, y0, x1, y1] = p.vp.convertToViewportRectangle(sp.bbox);
    const el = document.createElement('div');
    el.className = 'edit-span';
    el.dataset.page = String(pageIdx);
    el.dataset.idx = String(i);
    el.style.left = `${Math.min(x0, x1)}px`;
    el.style.top = `${Math.min(y0, y1)}px`;
    el.style.width = `${Math.abs(x1 - x0)}px`;
    el.style.height = `${Math.abs(y1 - y0)}px`;
    frag.appendChild(el);
  });
  p.editEl.replaceChildren(frag);
}

function cancelInlineEdit() {
  if (edit.active?.ta) { edit.active.ta.remove(); }
  edit.active = null;
  $('.edit-span.selected')?.classList.remove('selected');
}

function autosize(ta) {
  ta.style.height = 'auto';
  ta.style.height = `${Math.max(24, ta.scrollHeight + 2)}px`;
}

function syncToolbarFromSpan(span) {
  edit.font = guessFontId(span.font);
  edit.size = Math.round(span.size * 2) / 2;
  edit.color = rgbToHex(span.color || [0.07, 0.07, 0.07]);
  $('#edit-font').value = edit.font;
  $('#edit-size').value = String(edit.size);
  $('#edit-color').value = edit.color;
}

function openSpanEditor(pageIdx, idx) {
  cancelInlineEdit();
  const p = viewer.pages[pageIdx];
  const span = edit.spans.get(pageIdx)?.[idx];
  if (!p || !span) return;
  const [x0, y0, x1, y1] = p.vp.convertToViewportRectangle(span.bbox);
  const left = Math.min(x0, x1), top = Math.min(y0, y1);
  const w = Math.abs(x1 - x0), h = Math.abs(y1 - y0);

  syncToolbarFromSpan(span);

  const ta = document.createElement('textarea');
  ta.className = 'edit-textarea';
  ta.value = span.text;
  ta.rows = 1;
  Object.assign(ta.style, {
    left: `${left - 2}px`,
    top: `${top - 2}px`,
    minWidth: `${Math.max(48, w + 28)}px`,
    height: `${Math.max(20, h)}px`,
    fontSize: `${span.size * viewer.scale}px`,
    fontFamily: editCssFont(guessFontId(span.font)),
    color: edit.color,
  });
  p.editEl.appendChild(ta);
  $(`.edit-span[data-page="${pageIdx}"][data-idx="${idx}"]`)?.classList.add('selected');
  ta.focus();
  ta.select();
  autosize(ta);
  edit.active = { page: pageIdx, span, ta, kind: 'replace' };

  ta.addEventListener('blur', () => { if (edit.active?.ta === ta) commitSpanEditor(); });
  ta.addEventListener('input', () => autosize(ta));
  ta.addEventListener('keydown', (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); ta.blur(); }
    if (ev.key === 'Escape') { ev.preventDefault(); edit.active = null; ta.remove(); }
  });
}

async function commitSpanEditor() {
  const a = edit.active;
  if (!a) return;
  const text = a.ta.value;
  edit.active = null;
  a.ta.remove();
  const payload = {
    kind: text.trim() ? 'replace' : 'delete',
    page: a.page,
    rect: a.span.bbox,
    text,
    size: Number($('#edit-size').value) || a.span.size,
    font: $('#edit-font').value,
    color: $('#edit-color').value,
    direction: edit.dir,
  };
  await applyEdit(payload, a.page);
}

function startInsert() {
  if (!edit.on) return;
  edit.adding = !edit.adding;
  $('#edit-add').classList.toggle('active', edit.adding);
  $('#viewer-wrap').classList.toggle('adding', edit.adding);
  cancelInlineEdit();
  if (edit.adding) toast('在页面上点击要插入文字的位置', 'info', 2600);
}

function openInsertEditor(pageIdx, clientX, clientY) {
  cancelInlineEdit();
  const p = viewer.pages[pageIdx];
  if (!p || !p.vp) return;
  const br = p.boxEl.getBoundingClientRect();
  const at = { left: clientX - br.left, top: clientY - br.top };

  const size = Number($('#edit-size').value) || 14;
  const ta = document.createElement('textarea');
  ta.className = 'edit-textarea';
  ta.rows = 1;
  ta.placeholder = '输入文字…';
  Object.assign(ta.style, {
    left: `${at.left}px`,
    top: `${at.top}px`,
    minWidth: '130px',
    height: `${size * viewer.scale * 1.32}px`,
    fontSize: `${size * viewer.scale}px`,
    fontFamily: editCssFont($('#edit-font').value),
    color: $('#edit-color').value,
  });
  p.editEl.appendChild(ta);
  ta.focus();
  autosize(ta);
  edit.active = { page: pageIdx, ta, kind: 'insert', at };

  ta.addEventListener('blur', () => { if (edit.active?.ta === ta) commitInsert(); });
  ta.addEventListener('input', () => autosize(ta));
  ta.addEventListener('keydown', (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); ta.blur(); }
    if (ev.key === 'Escape') { ev.preventDefault(); edit.active = null; ta.remove(); }
  });
}

async function commitInsert() {
  const a = edit.active;
  if (!a) return;
  const text = a.ta.value;
  edit.active = null;
  a.ta.remove();
  if (!text.trim()) return;
  const p = viewer.pages[a.page];
  if (!p?.vp) return;
  const [x, y] = p.vp.convertToPdfPoint(a.at.left, a.at.top);
  await applyEdit({
    kind: 'insert',
    page: a.page,
    x, y, text,
    size: Number($('#edit-size').value) || 14,
    font: $('#edit-font').value,
    color: $('#edit-color').value,
    direction: edit.dir,
  }, a.page);
}

async function applyEdit(payload, pageIdx) {
  loading(true, '正在应用修改…');
  try {
    const meta = await call('apply_edit', payload);
    await refreshAfterEdit(meta, pageIdx);
    toast({ replace: '已修改', delete: '已删除', insert: '已插入' }[payload.kind] || '已应用');
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

/** 编辑后的轻量刷新：只重载受影响页 + 单张缩略图，不整篇重建 */
async function refreshAfterEdit(meta, pageIdx) {
  state.meta = meta;
  state.annotations = meta.annotations || [];
  markDirty(!!meta.dirty);
  $('#annot-count').textContent = String(state.annotations.length);
  renderAnnotList();

  const scroller = viewer.scroller;
  const prevTop = scroller.scrollTop;
  const boxTop = viewer.pages[pageIdx]?.boxEl.offsetTop ?? 0;
  const offset = Math.max(0, prevTop - boxTop);

  edit.spans.delete(pageIdx);
  await viewer.load(`doc.pdf?t=${encodeURIComponent(meta.token)}`, {
    annotations: state.annotations,
    restorePage: pageIdx,
  });
  const box = viewer.pages[Math.min(pageIdx, viewer.pages.length - 1)]?.boxEl;
  if (box) scroller.scrollTop = box.offsetTop + offset;

  const img = document.querySelector(`.thumb[data-page="${pageIdx}"] img`);
  if (img) img.src = `api/thumb/${pageIdx}?w=164&t=${encodeURIComponent(meta.token)}`;
}

async function doEditUndo() {
  cancelInlineEdit();
  try {
    const meta = await call('undo_edit');
    edit.spans.clear();
    await renderDoc(meta, { keepPage: true });
    toast('已撤销上一次编辑');
  } catch (err) { toastErr(err); }
}

/* ───────────────────────────── 事件绑定 ───────────────────────────── */

function bindEvents() {
  // 工具栏（含欢迎页按钮）
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (btn && !btn.disabled) {
      closeAllMenus();
      runAction(btn.dataset.act, btn);
      return;
    }
    // 菜单开关
    const menuBtn = e.target.closest('[data-menu]');
    if (menuBtn && !menuBtn.disabled) {
      const name = menuBtn.dataset.menu;
      const menu = document.querySelector(`.menu[data-menu-for="${name}"]`);
      const wasOpen = menu?.classList.contains('open');
      closeAllMenus();
      if (!wasOpen && menu) {
        menu.classList.add('open');
        if (name === 'more') refreshDefaultPdfMenu();
      }
      return;
    }
    if (!e.target.closest('.menu')) closeAllMenus();
  });

  // 工具切换
  $$('#tool-mode .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      state.tool = b.dataset.tool;
      $$('#tool-mode .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
      const wrap = $('#viewer-wrap');
      wrap.classList.toggle('tool-hand', state.tool === 'hand');
      wrap.classList.toggle('tool-annotate', state.tool === 'annotate');
      $('#float-toolbar').classList.remove('show');
    });
  });

  // 文字编辑：工具条 + 页面点击（捕获阶段，优先于其它处理）
  document.addEventListener('click', (e) => {
    if (!edit.on) return;
    if (e.target.closest('.editbar')) return;
    if (e.target.closest('.edit-textarea')) return;
    const spanEl = e.target.closest('.edit-span');
    if (spanEl) {
      e.preventDefault();
      e.stopPropagation();
      openSpanEditor(Number(spanEl.dataset.page), Number(spanEl.dataset.idx));
      return;
    }
    if (edit.adding) {
      const hit = viewer.pointToPdf(e.clientX, e.clientY);
      if (hit) {
        e.preventDefault();
        e.stopPropagation();
        openInsertEditor(hit.page, e.clientX, e.clientY);
      }
      return;
    }
    if (edit.active) { e.preventDefault(); cancelInlineEdit(); }
  }, true);

  $('#edit-add')?.addEventListener('click', startInsert);
  $('#edit-undo')?.addEventListener('click', doEditUndo);
  $('#edit-done')?.addEventListener('click', () => toggleEdit(false));
  $('#edit-font')?.addEventListener('change', (e) => { edit.font = e.target.value; });
  $('#edit-size')?.addEventListener('change', (e) => { edit.size = Number(e.target.value) || 14; });
  $('#edit-color')?.addEventListener('input', (e) => { edit.color = e.target.value; });
  $$('#edit-dir .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      edit.dir = b.dataset.dir;
      $$('#edit-dir .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
    });
  });

  // 缩放标签：点击输入
  $('#zoom-label').addEventListener('click', async () => {
    const v = await askText({ title: '缩放比例', label: '输入百分比（15 ~ 600）', value: String(Math.round(state.scale * 100)) });
    const n = parseFloat(v);
    if (n && n >= 15 && n <= 600) viewer.setScale(n / 100);
  });

  // 侧栏 tab
  $$('.side-tab').forEach((t) => {
    t.addEventListener('click', () => {
      $$('.side-tab').forEach((x) => x.classList.toggle('active', x === t));
      $$('.side-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === t.dataset.panel));
    });
  });

  // 缩略图交互
  const thumbs = $('#thumbs');
  thumbs.addEventListener('click', (e) => {
    const t = e.target.closest('.thumb');
    if (!t) return;
    const i = Number(t.dataset.page);
    if (e.ctrlKey || e.metaKey) {
      state.selected.has(i) ? state.selected.delete(i) : state.selected.add(i);
      refreshThumbSelection();
    } else if (e.shiftKey && state.page != null) {
      const [a, b] = [Math.min(state.page, i), Math.max(state.page, i)];
      for (let k = a; k <= b; k++) state.selected.add(k);
      refreshThumbSelection();
    } else {
      state.selected.clear();
      refreshThumbSelection();
      viewer.gotoPage(i);
    }
  });

  let thumbMenuPage = null;
  thumbs.addEventListener('contextmenu', (e) => {
    const t = e.target.closest('.thumb');
    if (!t) return;
    e.preventDefault();
    thumbMenuPage = Number(t.dataset.page);
    if (!state.selected.has(thumbMenuPage)) {
      state.selected.clear();
      state.selected.add(thumbMenuPage);
      refreshThumbSelection();
    }
    const menu = $('#thumb-menu');
    menu.hidden = false;
    menu.classList.add('open');
    const x = Math.min(e.clientX, window.innerWidth - 190);
    const y = Math.min(e.clientY, window.innerHeight - 240);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
  });

  document.addEventListener('mousedown', (e) => {
    const menu = $('#thumb-menu');
    if (!menu.hidden && !e.target.closest('#thumb-menu')) {
      menu.hidden = true;
      menu.classList.remove('open');
    }
  });

  $('#thumb-menu').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-thumb-act]');
    if (!b) return;
    const act = b.dataset.thumbAct;
    const sel = [...state.selected];
    $('#thumb-menu').hidden = true;
    if (act === 'rotate-cw') await doRotate(90, sel);
    else if (act === 'rotate-ccw') await doRotate(-90, sel);
    else if (act === 'delete') await doDeletePages();
    else if (act === 'extract') await doExtractPages();
    else if (act === 'select-all') {
      state.selected = new Set(Array.from({ length: state.meta?.pages || 0 }, (_, i) => i));
      refreshThumbSelection();
    } else if (act === 'clear-sel') {
      state.selected.clear();
      refreshThumbSelection();
    }
  });

  // 缩略图拖拽重排
  let dragFrom = null;
  thumbs.addEventListener('dragstart', (e) => {
    const t = e.target.closest('.thumb');
    if (!t) return;
    dragFrom = Number(t.dataset.page);
    t.style.opacity = '.4';
    e.dataTransfer.effectAllowed = 'move';
  });
  thumbs.addEventListener('dragend', (e) => {
    e.target.closest('.thumb')?.style.removeProperty('opacity');
    dragFrom = null;
  });
  thumbs.addEventListener('dragover', (e) => {
    const t = e.target.closest('.thumb');
    if (!t || dragFrom === null) return;
    e.preventDefault();
    t.style.borderTop = '2px solid var(--accent)';
  });
  thumbs.addEventListener('dragleave', (e) => {
    e.target.closest('.thumb')?.style.removeProperty('border-top');
  });
  thumbs.addEventListener('drop', async (e) => {
    const t = e.target.closest('.thumb');
    if (!t || dragFrom === null) return;
    e.preventDefault();
    e.stopPropagation();
    t.style.removeProperty('border-top');
    const to = Number(t.dataset.page);
    const from = dragFrom;
    dragFrom = null;
    await doMovePage(from, to);
  });

  // 缩略图需要 draggable
  new MutationObserver(() => {
    $$('.thumb').forEach((t) => { t.draggable = true; });
  }).observe(thumbs, { childList: true });

  // 选区
  $('#viewer-wrap').addEventListener('mouseup', (e) => {
    if (e.target.closest('#float-toolbar')) return;
    onSelectionSettled();
  });
  document.addEventListener('selectionchange', () => {
    if (!window.getSelection()?.isCollapsed) onSelectionSettled();
  });
  $('#float-toolbar').addEventListener('mousedown', (e) => e.preventDefault());
  $('#float-toolbar').addEventListener('click', (e) => {
    const b = e.target.closest('[data-fmt]');
    if (b) applyFormat(b.dataset.fmt);
  });

  // 搜索
  $('#search-input').addEventListener('input', debounce((e) => runSearch(e.target.value.trim()), 380));
  $('#search-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') stepSearch(e.shiftKey ? -1 : 1);
    if (e.key === 'Escape') toggleSearch(false);
  });
  $('#search-next').addEventListener('click', () => stepSearch(1));
  $('#search-prev').addEventListener('click', () => stepSearch(-1));
  $('#search-close').addEventListener('click', () => toggleSearch(false));

  // 手型工具拖动画布
  const pagesEl = $('#pages');
  let panning = null;
  pagesEl.addEventListener('mousedown', (e) => {
    if (state.tool !== 'hand' || e.button !== 0) return;
    panning = { x: e.clientX, y: e.clientY, left: pagesEl.scrollLeft, top: pagesEl.scrollTop };
    pagesEl.classList.add('grabbing');
    e.preventDefault();
  });
  window.addEventListener('mousemove', (e) => {
    if (!panning) return;
    pagesEl.scrollLeft = panning.left - (e.clientX - panning.x);
    pagesEl.scrollTop = panning.top - (e.clientY - panning.y);
  });
  window.addEventListener('mouseup', () => {
    panning = null;
    pagesEl.classList.remove('grabbing');
  });

  // Ctrl + 滚轮缩放
  pagesEl.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    viewer.setScale(state.scale * (e.deltaY < 0 ? 1.1 : 0.9));
  }, { passive: false });

  // 点击批注气泡 / pin
  $('#viewer-wrap').addEventListener('click', (e) => {
    const mark = e.target.closest('.annot-mark');
    if (!mark) return;
    const a = state.annotations.find((x) => x.id === mark.dataset.aid);
    if (!a) return;
    showAnnotBubble(a, mark);
  });

  // 拖放打开
  const wrap = $('#viewer-wrap');
  const dz = $('#dropzone');
  let dragDepth = 0;
  wrap.addEventListener('dragenter', (e) => {
    if (!e.dataTransfer?.types.includes('Files')) return;
    dragDepth++;
    dz.classList.add('show');
  });
  wrap.addEventListener('dragover', (e) => { e.preventDefault(); });
  wrap.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dz.classList.remove('show');
  });
  wrap.addEventListener('drop', async (e) => {
    const file = e.dataTransfer?.files?.[0];
    dz.classList.remove('show');
    if (!file) return;
    e.preventDefault();
    e.stopPropagation();
    if (!/\.pdf$/i.test(file.name)) {
      toast('目前只支持拖入 PDF 文件', 'err');
      return;
    }
    await uploadAndOpen(file);
  });

  // 兜底：拖到窗口任意位置（顶栏/侧栏/缩略图区）也能打开，
  // 并阻止 WebView2 的默认行为（导航到被拖入的文件）。
  document.addEventListener('dragover', (e) => {
    if (e.dataTransfer?.types?.includes('Files')) e.preventDefault();
  });
  document.addEventListener('drop', (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    e.preventDefault();
    if (!/\.pdf$/i.test(file.name)) {
      toast('目前只支持拖入 PDF 文件', 'err');
      return;
    }
    if (typeof uploadAndOpen === 'function') uploadAndOpen(file);
  });

  // 侧栏宽度拖拽
  const resizer = $('#resizer');
  let resizing = false;
  resizer.addEventListener('mousedown', () => {
    resizing = true;
    resizer.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
  });
  window.addEventListener('mousemove', (e) => {
    if (!resizing) return;
    const w = Math.max(150, Math.min(420, e.clientX));
    document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
  });
  window.addEventListener('mouseup', () => {
    if (!resizing) return;
    resizing = false;
    resizer.classList.remove('dragging');
    document.body.style.cursor = '';
    viewer?.fitWidth();
  });

  // 快捷键
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);

    if (mod && e.key.toLowerCase() === 'o') { e.preventDefault(); doOpen(); }
    else if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); e.shiftKey ? doSaveAs() : doSave(); }
    else if (mod && e.key.toLowerCase() === 'f') { e.preventDefault(); toggleSearch(true); }
    else if (mod && (e.key === '=' || e.key === '+')) { e.preventDefault(); viewer.setScale(state.scale * 1.2); }
    else if (mod && e.key === '-') { e.preventDefault(); viewer.setScale(state.scale / 1.2); }
    else if (mod && e.key === '0') { e.preventDefault(); viewer.fitPage(); }
    else if (mod && e.key === 'p') { e.preventDefault(); doPrint(); }
    else if (mod && e.key.toLowerCase() === 'h' && state.pendingSel) { e.preventDefault(); applyFormat('highlight'); }
    else if (mod && e.key.toLowerCase() === 'z' && !typing) {
      e.preventDefault();
      if (edit.on) doEditUndo();
      else if ((state.meta?.undo || 0) > 0) doUndo();
    }
    else if (typing) return;
    else if (e.key === 'v' || e.key === 'V') setTool('select');
    else if (e.key === 'h' || e.key === 'H') setTool('hand');
    else if (e.key === 'n' || e.key === 'N') setTool('annotate');
    else if (e.key === 'e' || e.key === 'E') { if (state.meta) toggleEdit(); }
    else if (e.key === 'PageDown' || e.key === 'ArrowRight') viewer.gotoPage(state.page + 1);
    else if (e.key === 'PageUp' || e.key === 'ArrowLeft') viewer.gotoPage(state.page - 1);
    else if (e.key === 'Home') viewer.gotoPage(0);
    else if (e.key === 'End') viewer.gotoPage(viewer.pages.length - 1);
    else if (e.key === 'Delete' && state.meta) doDeletePages();
    else if (e.key === 'Escape') {
      closeAllMenus();
      $('#float-toolbar').classList.remove('show');
      toggleSearch(false);
      if (edit.on) {
        if (edit.adding) startInsert();       // 先退出"添加文字"
        else toggleEdit(false);               // 再退出编辑模式
      }
    }
  });

  window.addEventListener('beforeunload', (e) => {
    if (state.meta && !state.meta.empty && $('#st-dirty') && !$('#st-dirty').hidden) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
}

function setTool(tool) {
  const b = document.querySelector(`#tool-mode [data-tool="${tool}"]`);
  if (b) b.click();
}

function closeAllMenus() {
  $$('.menu.open').forEach((m) => m.classList.remove('open'));
}

function toggleSearch(show) {
  const box = $('#searchbox');
  if (!show) { box.hidden = true; viewer?.clearSearchHits(); return; }
  if (!state.meta || state.meta.empty) return;
  box.hidden = false;
  setTimeout(() => $('#search-input').focus(), 30);
}

function showAnnotBubble(a, anchor) {
  document.querySelectorAll('.annot-bubble').forEach((n) => n.remove());
  if (!a.content) return;
  const bubble = document.createElement('div');
  bubble.className = 'annot-bubble';
  bubble.innerHTML = `<div>${escapeHtml(a.content)}</div>
    <div class="bubble-foot"><button class="btn-ghost" style="height:25px;padding:0 10px;font-size:11.5px">删除</button></div>`;
  const wrap = $('#viewer-wrap').getBoundingClientRect();
  const r = anchor.getBoundingClientRect();
  bubble.style.left = `${Math.min(r.left - wrap.left, wrap.width - 280)}px`;
  bubble.style.top = `${r.bottom - wrap.top + 6}px`;
  $('#viewer-wrap').appendChild(bubble);
  bubble.querySelector('button').addEventListener('click', async () => {
    bubble.remove();
    await removeAnnot(a.id);
  });
  setTimeout(() => {
    const off = (ev) => {
      if (!bubble.contains(ev.target)) { bubble.remove(); document.removeEventListener('mousedown', off); }
    };
    document.addEventListener('mousedown', off);
  }, 10);
}

async function uploadAndOpen(file) {
  loading(true, '正在读取文件…');
  try {
    // 先落到本地临时文件，再走统一打开流程
    const buf = await file.arrayBuffer();
    const resp = await fetch(`api/upload?name=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      body: buf,
    });
    const json = await resp.json();
    if (!json?.ok) throw new Error(json?.error || '上传失败');
    const meta = await call('open_path', json.path, '');
    await renderDoc(meta, { keepPage: false });
    toast(`已打开 ${file.name}`);
  } catch (err) { toastErr(err); }
  finally { loading(false); }
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/* ───────────────────────────── 启动 ───────────────────────────── */

async function boot() {
  viewer = new Viewer($('#pages'), $('#pages-inner'), {
    onPageChange: (i) => {
      state.page = i;
      $('#st-page').textContent = state.meta?.pages ? `${i + 1} / ${state.meta.pages}` : '— / —';
      refreshThumbSelection();
      const cur = document.querySelector(`.thumb[data-page="${i}"]`);
      cur?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    },
    onScale: (s) => {
      state.scale = s;
      $('#zoom-label').textContent = `${Math.round(s * 100)}%`;
    },
    onPageRendered: (i) => { if (edit.on) ensureEditSpans(i); },
  });

  bindEvents();

  try {
    await waitBackend();
    const st = await call('get_state');
    renderRecent(st.recent);
    if (!st.empty) {
      await renderDoc(st, { keepPage: false });
    } else {
      syncChrome();
    }
    if (st.ocr === false) {
      toast('提示：OCR 组件未就绪，扫描件识别功能暂不可用', 'info', 5000);
    }
  } catch (err) {
    console.error(err);
    toastErr(err);
    syncChrome();
  }
}

boot();
