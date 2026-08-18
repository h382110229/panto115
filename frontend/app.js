/**
 * Panto115 — 前端交互逻辑 (原生 JS, 零依赖)
 */

(function () {
  'use strict';

  // DOM
  const $ = (s) => document.querySelector(s);
  const searchInput = $('#search-input');
  const searchBtn = $('#search-btn');
  const statusEl = $('#status-indicator');
  const resultsList = $('#results-list');
  const resultsInfo = $('#results-info');
  const skeleton = $('#skeleton');
  const emptyState = $('#empty-state');
  const tags = document.querySelectorAll('.tag');

  let currentPan = 'all';
  let debounceTimer = null;

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
  // API helpers
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
          ? ` · ${data.space_used} / ${data.space_total}`
          : '';
        statusEl.querySelector('.status-text').textContent =
          `${data.user_name || '已登录'}${space}`;
      } else {
        statusEl.className = 'status-badge offline';
        statusEl.querySelector('.status-text').textContent =
          data.error ? `未登录: ${data.error.slice(0, 20)}` : '未登录';
      }
    } catch {
      statusEl.className = 'status-badge offline';
      statusEl.querySelector('.status-text').textContent = '服务不可用';
    }
  }

  // -----------------------------------------------------------------------
  // Search
  // -----------------------------------------------------------------------
  async function doSearch(query) {
    const q = query || searchInput.value.trim();
    if (!q) return;

    // 磁力链接或 115 链接 → 直接转存
    if (/^magnet:/i.test(q) || /115\.com\/s\//.test(q) || /^[a-zA-Z0-9]{12,16}$/.test(q)) {
      return doSave(q);
    }

    searchInput.value = q;
    showLoading(true);

    try {
      const { success, data } = await api(
        `/search?q=${encodeURIComponent(q)}&pan=${currentPan}`
      );
      showLoading(false);

      if (!success || !data) {
        showToast('搜索失败', 'error');
        renderResults([]);
        return;
      }

      renderResults(data.results || [], data.total, data.elapsed_ms);
    } catch (e) {
      showLoading(false);
      showToast(`搜索异常: ${e.message}`, 'error');
      renderResults([]);
    }
  }

  // -----------------------------------------------------------------------
  // Save
  // -----------------------------------------------------------------------
  async function doSave(url, extractCode = '') {
    if (!url) return;
    showToast('正在转存...', 'warning', 2000);

    try {
      const { success, message, data } = await api('/save', {
        method: 'POST',
        body: JSON.stringify({ url, extract_code: extractCode }),
      });

      if (success) {
        showToast(message || '转存成功!', 'success');
      } else {
        const msg = message || data?.message || '转存失败';
        showToast(msg, msg.includes('暂不支持') ? 'warning' : 'error', 4000);
      }
    } catch (e) {
      showToast(`转存异常: ${e.message}`, 'error');
    }
  }

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------
  function showLoading(show) {
    skeleton.classList.toggle('hidden', !show);
    if (show) {
      resultsList.innerHTML = '';
      resultsInfo.classList.add('hidden');
      emptyState.classList.add('hidden');
    }
  }

  function renderResults(results, total, ms) {
    resultsInfo.classList.remove('hidden');
    emptyState.classList.add('hidden');

    if (!results || results.length === 0) {
      resultsList.innerHTML = '';
      resultsInfo.textContent = '无结果';
      emptyState.classList.remove('hidden');
      return;
    }

    resultsInfo.innerHTML = `
      <span>共 <strong>${total}</strong> 条结果</span>
      <span>${ms || 0}ms</span>
    `;

    resultsList.innerHTML = results.map((r) => `
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
          <button class="btn-save" onclick="Panto115.save(this,'${escAttr(r.share_url)}','${escAttr(r.extract_code || '')}')">一键转存</button>
          <a class="btn-open" href="${escAttr(r.share_url)}" target="_blank" rel="noopener">打开</a>
        </div>
      </div>
    `).join('');
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
  function escAttr(s) { return (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;'); }

  // -----------------------------------------------------------------------
  // Events
  // -----------------------------------------------------------------------
  searchBtn.addEventListener('click', () => doSearch());
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doSearch();
  });

  // Filter tags
  tags.forEach((tag) => {
    tag.addEventListener('click', () => {
      tags.forEach((t) => t.classList.remove('active'));
      tag.classList.add('active');
      currentPan = tag.dataset.pan;
      if (searchInput.value.trim()) doSearch();
    });
  });

  // -----------------------------------------------------------------------
  // Init
  // -----------------------------------------------------------------------
  checkStatus();

  // 暴露全局方法给 onclick
  window.Panto115 = {
    async save(btn, url, code) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      await doSave(url, code);
      btn.disabled = false;
      btn.textContent = '一键转存';
    },
  };
})();
