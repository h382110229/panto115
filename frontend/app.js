/**
 * Panto115 — 前端交互逻辑 (原生 JS, 零依赖)
 *
 * 骨架屏策略: 仅在 search() 触发时动态生成，搜索结束立即替换为真实结果。
 * 页面初始只显示空状态欢迎语，绝无静态骨架屏。
 */

(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const searchInput = $('#search-input');
  const searchBtn = $('#search-btn');
  const statusEl = $('#status-indicator');
  const container = $('#results-container');
  const resultsInfo = $('#results-info');
  const tags = document.querySelectorAll('.tag');

  let currentPan = 'all';

  // -----------------------------------------------------------------------
  // Toast
  // -----------------------------------------------------------------------
  function showToast(msg, type = 'success', duration = 3000) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = `toast ${type}`;
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add('hidden'), duration);
  }

  // -----------------------------------------------------------------------
  // API helper
  // -----------------------------------------------------------------------
  async function api(path, opts = {}) {
    const resp = await fetch(`/api${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    return resp.json();
  }

  // -----------------------------------------------------------------------
  // Status
  // -----------------------------------------------------------------------
  async function checkStatus() {
    statusEl.className = 'status-badge checking';
    statusEl.querySelector('.status-text').textContent = '检测中...';
    try {
      const { success, data } = await api('/status');
      if (success && data.logged_in) {
        statusEl.className = 'status-badge online';
        const space = data.space_used && data.space_total
          ? ` · ${data.space_used} / ${data.space_total}` : '';
        statusEl.querySelector('.status-text').textContent =
          `${data.user_name || '已登录'}${space}`;
      } else {
        statusEl.className = 'status-badge offline';
        statusEl.querySelector('.status-text').textContent =
          data.error ? `未登录` : '未登录';
      }
    } catch {
      statusEl.className = 'status-badge offline';
      statusEl.querySelector('.status-text').textContent = '服务不可用';
    }
  }

  // -----------------------------------------------------------------------
  // 骨架屏 — 仅由 JS 动态生成
  // -----------------------------------------------------------------------
  function showSkeleton() {
    resultsInfo.classList.add('hidden');
    container.innerHTML = `
      <div class="skeleton-grid">
        ${Array(3).fill(`
          <div class="skeleton-card">
            <div class="skeleton-line w60"></div>
            <div class="skeleton-line w80"></div>
            <div class="skeleton-line w40"></div>
          </div>
        `).join('')}
      </div>`;
  }

  // -----------------------------------------------------------------------
  // 空状态
  // -----------------------------------------------------------------------
  function showEmpty() {
    resultsInfo.classList.add('hidden');
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🔍</div>
        <p>输入关键词搜索网盘资源，或直接粘贴 magnet 磁力 / 115 分享链接回车</p>
        <p class="empty-hint">支持 115、夸克、阿里云盘、百度网盘等资源聚合搜索</p>
      </div>`;
  }

  // -----------------------------------------------------------------------
  // 搜索
  // -----------------------------------------------------------------------
  async function doSearch(query) {
    const q = query || searchInput.value.trim();
    if (!q) return;

    // 磁力链接或 115 链接 → 直接转存
    if (/^magnet:/i.test(q) || /115\.com\/s\//.test(q) || /^[a-zA-Z0-9]{12,16}$/.test(q)) {
      return doSave(q);
    }

    searchInput.value = q;
    showSkeleton(); // 搜索开始时才显示骨架屏

    try {
      const { success, data } = await api(
        `/search?q=${encodeURIComponent(q)}&pan=${currentPan}`
      );

      if (!success || !data) {
        renderResults([], 0, 0, '搜索失败');
        return;
      }
      renderResults(data.results || [], data.total, data.elapsed_ms, null, data.errors);
    } catch (e) {
      renderResults([], 0, 0, `搜索异常: ${e.message}`);
    }
  }

  // -----------------------------------------------------------------------
  // 转存
  // -----------------------------------------------------------------------
  async function doSave(url, extractCode = '') {
    if (!url) return;
    showToast('正在转存...', 'warning', 2000);
    try {
      const { success, message } = await api('/save', {
        method: 'POST',
        body: JSON.stringify({ url, extract_code: extractCode }),
      });
      showToast(message || (success ? '转存成功!' : '转存失败'),
        success ? 'success' : (message && message.includes('暂不支持') ? 'warning' : 'error'),
        success ? 3000 : 4000);
    } catch (e) {
      showToast(`转存异常: ${e.message}`, 'error');
    }
  }

  // -----------------------------------------------------------------------
  // 渲染结果
  // -----------------------------------------------------------------------
  function renderResults(results, total, ms, error, errors) {
    resultsInfo.classList.remove('hidden');

    if (error) {
      container.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><p>${esc(error)}</p></div>`;
      resultsInfo.textContent = '';
      return;
    }

    if (!results.length) {
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">😕</div>
          <p>未找到相关资源</p>
          <p class="empty-hint">尝试换个关键词，或直接粘贴磁力/分享链接</p>
        </div>`;
      resultsInfo.innerHTML = `<span>0 条结果</span><span>${ms || 0}ms</span>`;
      return;
    }

    // 源分布统计
    const srcMap = {};
    results.forEach(r => { srcMap[r.source] = (srcMap[r.source] || 0) + 1; });
    const srcTags = Object.entries(srcMap).map(([k, v]) => `<span class="source-badge">${k}:${v}</span>`).join(' ');

    resultsInfo.innerHTML = `
      <span>共 <strong>${total}</strong> 条结果 · ${ms || 0}ms ${srcTags}</span>`;

    container.innerHTML = `<div class="results-grid">${results.map(r => `
      <div class="result-card">
        <div class="result-info">
          <div class="result-top">
            <span class="pan-badge pan-${r.pan_type}">${r.pan_type}</span>
            <span class="result-title" title="${esc(r.title)}">${esc(r.title)}</span>
            <span class="source-badge">${esc(r.source)}</span>
          </div>
          <div class="result-meta">
            ${r.datetime ? `<span>${esc(r.datetime)}</span>` : ''}
            ${r.extract_code ? `<span class="extract-code">提取码: ${esc(r.extract_code)}</span>` : ''}
          </div>
        </div>
        <div class="result-actions">
          <button class="btn-save" onclick="P115.save(this,'${escAttr(r.share_url)}','${escAttr(r.extract_code || '')}')">一键转存</button>
          <a class="btn-open" href="${escAttr(r.share_url)}" target="_blank" rel="noopener">打开</a>
        </div>
      </div>
    `).join('')}</div>`;

    if (errors && errors.length) {
      showToast(`部分源异常: ${errors.join('; ')}`, 'warning', 4000);
    }
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function escAttr(s) { return (s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;'); }

  // -----------------------------------------------------------------------
  // Events
  // -----------------------------------------------------------------------
  searchBtn.addEventListener('click', () => doSearch());
  searchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

  tags.forEach((tag) => {
    tag.addEventListener('click', () => {
      tags.forEach((t) => t.classList.remove('active'));
      tag.classList.add('active');
      currentPan = tag.dataset.pan;
      if (searchInput.value.trim()) doSearch();
    });
  });

  // -----------------------------------------------------------------------
  // Init — 只显示空状态，绝不显示骨架屏
  // -----------------------------------------------------------------------
  showEmpty();
  checkStatus();

  window.P115 = {
    async save(btn, url, code) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      await doSave(url, code);
      btn.disabled = false;
      btn.textContent = '一键转存';
    },
  };
})();
