(() => {
  "use strict";

  const PAGE_SIZE = 12;
  const TAG_ORDER = ["全部", "通知", "科研", "人物", "活动", "校园"];

  const state = {
    data: null,
    articles: [],
    filtered: [],
    query: "",
    tag: "全部",
    sort: "new",
    page: 1,
    currentId: null,
    lastFocus: null,
  };

  const els = {
    syncPill: document.getElementById("syncPill"),
    syncText: document.getElementById("syncText"),
    metricTotal: document.getElementById("metricTotal"),
    metricWechat: document.getElementById("metricWechat"),
    metricUpdated: document.getElementById("metricUpdated"),
    searchInput: document.getElementById("searchInput"),
    filterChips: document.getElementById("filterChips"),
    sortSelect: document.getElementById("sortSelect"),
    featuredSlot: document.getElementById("featuredSlot"),
    articleGrid: document.getElementById("articleGrid"),
    emptyState: document.getElementById("emptyState"),
    emptyTitle: document.getElementById("emptyTitle"),
    emptyText: document.getElementById("emptyText"),
    emptyAction: document.getElementById("emptyAction"),
    loadMore: document.getElementById("loadMore"),
    resultMeta: document.getElementById("resultMeta"),
    themeToggle: document.getElementById("themeToggle"),
    reader: document.getElementById("reader"),
    readerPanel: document.querySelector(".reader-panel"),
    readerProgress: document.getElementById("readerProgress"),
    readerSource: document.getElementById("readerSource"),
    readerScroll: document.getElementById("readerScroll"),
    readerDate: document.getElementById("readerDate"),
    readerTitle: document.getElementById("readerTitle"),
    readerTags: document.getElementById("readerTags"),
    readerCover: document.getElementById("readerCover"),
    readerBody: document.getElementById("readerBody"),
    readerFooter: document.getElementById("readerFooter"),
    readerLinks: document.getElementById("readerLinks"),
    readerCopy: document.getElementById("readerCopy"),
    readerClose: document.getElementById("readerClose"),
    footerYear: document.getElementById("footerYear"),
    footerSync: document.getElementById("footerSync"),
    toast: document.getElementById("toast"),
  };

  const ICONS = {
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13m-5-5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    external: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);

  /* ------------------------------------------------------------------ */
  /* 日期                                                                */
  /* ------------------------------------------------------------------ */

  function dateParts(iso) {
    if (!iso) return null;
    const match = String(iso).match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!match) return null;
    return { year: match[1], month: match[2], day: match[3], hour: match[4], minute: match[5] };
  }

  function shortDate(iso) {
    const p = dateParts(iso);
    return p ? `${p.month}.${p.day}` : "—";
  }

  function shortDateTime(iso) {
    const p = dateParts(iso);
    return p ? `${p.month}.${p.day} ${p.hour}:${p.minute}` : "";
  }

  function fullDate(iso) {
    const p = dateParts(iso);
    return p ? `${p.year}年${p.month}月${p.day}日 ${p.hour}:${p.minute}` : "";
  }

  function relativeDate(iso) {
    if (!iso) return "";
    const time = new Date(iso).getTime();
    if (!Number.isFinite(time)) return "";
    const diff = (Date.now() - time) / 86400000;
    if (diff < 0) return "";
    if (diff < 1) return "今天";
    if (diff < 2) return "昨天";
    if (diff < 8) return `${Math.floor(diff)} 天前`;
    return "";
  }

  function cardDate(iso) {
    return relativeDate(iso) || shortDate(iso);
  }

  /* ------------------------------------------------------------------ */
  /* 通用                                                                */
  /* ------------------------------------------------------------------ */

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  let toastTimer = null;
  function toast(message) {
    els.toast.textContent = message;
    els.toast.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove("is-visible"), 2200);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      try {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        const ok = document.execCommand("copy");
        area.remove();
        return ok;
      } catch (fallbackError) {
        return false;
      }
    }
  }

  function sourceLabel(article) {
    return article && article.source === "paper" ? "官方澎湃号" : "微信原文";
  }

  function tagMarkup(tags) {
    return (tags || []).map((tag) => `<span class="tag" data-tag="${esc(tag)}">${esc(tag)}</span>`).join("");
  }

  function coverMarkup(article, eager = false) {
    if (article && article.cover) {
      return `<img data-cover src="${esc(article.cover)}" alt="" loading="${eager ? "eager" : "lazy"}" decoding="async" referrerpolicy="no-referrer">`;
    }
    return '<div class="cover-placeholder" aria-hidden="true"></div>';
  }

  function matchesQuery(article, query) {
    if (!query) return true;
    const haystack = `${article.title || ""} ${article.summary || ""} ${(article.tags || []).join(" ")}`.toLowerCase();
    return query.toLowerCase().split(/\s+/).filter(Boolean).every((token) => haystack.includes(token));
  }

  function compareArticles(a, b) {
    const left = a.published_at || "";
    const right = b.published_at || "";
    if (left === right) return String(a.id).localeCompare(String(b.id));
    return left < right ? -1 : 1;
  }

  /* ------------------------------------------------------------------ */
  /* 数据与元信息                                                        */
  /* ------------------------------------------------------------------ */

  function applyMeta() {
    const data = state.data || {};
    const account = data.account || {};
    const articles = state.articles;
    const stats = data.stats || {};

    els.metricTotal.textContent = articles.length ? String(articles.length) : "—";
    els.metricWechat.textContent = articles.length ? String(stats.wechat ?? articles.filter((item) => item.source === "wechat").length) : "—";

    const latest = articles.length ? articles[0].published_at : null;
    els.metricUpdated.textContent = latest ? shortDate(latest) : "—";
    els.footerSync.textContent = latest ? `最近更新 ${fullDate(latest)}` : "等待首次同步";

    const repo = account.repo_url || "https://github.com/iCxin/scau-wechat-mirror";
    document.querySelectorAll("[data-repo-link]").forEach((link) => {
      const href = link.getAttribute("href") || "";
      if (href.includes("/actions") || href.includes("/issues")) return;
      link.href = repo;
    });

    const lastSync = data.sync && data.sync.last_success ? data.sync.last_success : data.generated_at;
    els.syncPill.classList.remove("is-stale", "is-error");
    if (!lastSync) {
      els.syncText.textContent = "等待首次同步";
      els.syncPill.classList.add("is-stale");
      return;
    }
    els.syncText.textContent = `更新于 ${shortDateTime(lastSync)}`;
    const age = Date.now() - new Date(lastSync).getTime();
    if (Number.isFinite(age) && age > 48 * 86400000) {
      els.syncPill.classList.add("is-stale");
      els.syncPill.title = "超过 48 小时没有更新，请检查 GitHub Actions";
    }
    if (data.sync && data.sync.last_error && !data.sync.last_success) {
      els.syncPill.classList.add("is-error");
      els.syncPill.title = String(data.sync.last_error);
    }
  }

  function buildFilters() {
    const counts = new Map();
    state.articles.forEach((article) => {
      (article.tags || []).forEach((tag) => counts.set(tag, (counts.get(tag) || 0) + 1));
    });

    const extras = [...counts.keys()].filter((tag) => !TAG_ORDER.includes(tag)).sort();
    const tags = TAG_ORDER.filter((tag) => tag === "全部" || counts.has(tag)).concat(extras);
    if (!tags.includes(state.tag)) state.tag = "全部";

    els.filterChips.innerHTML = tags.map((tag) => {
      const count = tag === "全部" ? state.articles.length : counts.get(tag) || 0;
      return `<button class="chip ${state.tag === tag ? "is-active" : ""}" type="button" data-tag="${esc(tag)}" aria-pressed="${state.tag === tag}">${esc(tag)}<span class="chip-count">${count}</span></button>`;
    }).join("");
  }

  function syncChips() {
    [...els.filterChips.querySelectorAll(".chip")].forEach((chip) => {
      const active = chip.dataset.tag === state.tag;
      chip.classList.toggle("is-active", active);
      chip.setAttribute("aria-pressed", String(active));
    });
  }

  /* ------------------------------------------------------------------ */
  /* 列表渲染                                                            */
  /* ------------------------------------------------------------------ */

  function computeFiltered() {
    let list = state.articles.filter((article) => state.tag === "全部" || (article.tags || []).includes(state.tag));
    list = list.filter((article) => matchesQuery(article, state.query));
    list.sort((a, b) => (state.sort === "new" ? compareArticles(b, a) : compareArticles(a, b)));
    state.filtered = list;
  }

  function featuredMarkup(article) {
    return `
      <a class="featured-card" href="#article=${encodeURIComponent(article.id)}" data-open="${esc(article.id)}">
        <div class="featured-cover">${coverMarkup(article, true)}</div>
        <div class="featured-body">
          <span class="featured-label">LATEST PUSH / 最新推送</span>
          <h3>${esc(article.title)}</h3>
          <p class="featured-summary">${esc(article.summary || "点击阅读本期推文全文。")}</p>
          <div class="tags-row">${tagMarkup(article.tags)}</div>
          <div class="featured-meta">
            <span>${esc(cardDate(article.published_at))} · ${esc(sourceLabel(article))}</span>
            <span class="read-link">阅读全文 ${ICONS.arrow}</span>
          </div>
        </div>
      </a>`;
  }

  function cardMarkup(article, index) {
    const words = Number(article.word_count) > 0 ? `${article.word_count} 字` : "";
    return `
      <a class="article-card" href="#article=${encodeURIComponent(article.id)}" data-open="${esc(article.id)}" style="--i:${index}">
        <div class="card-cover">${coverMarkup(article)}</div>
        <div class="card-body">
          <div class="card-meta">
            <span>${esc(cardDate(article.published_at))}</span>
            <span class="card-source ${article.source === "paper" ? "is-paper" : ""}">${esc(sourceLabel(article))}</span>
          </div>
          <h3 class="card-title">${esc(article.title)}</h3>
          <p class="card-summary">${esc(article.summary || "")}</p>
          <div class="tags-row">${tagMarkup(article.tags)}</div>
          <div class="card-footer">
            <span class="read">阅读全文 ${ICONS.arrow}</span>
            <span>${esc(words)}</span>
          </div>
        </div>
      </a>`;
  }

  function updateResultMeta(total) {
    if (!state.articles.length) {
      els.resultMeta.textContent = "等待首次同步";
      return;
    }
    if (state.query || state.tag !== "全部") {
      els.resultMeta.textContent = `共 ${state.articles.length} 篇 · 筛选到 ${total} 篇`;
    } else {
      const latest = state.articles[0].published_at;
      els.resultMeta.textContent = `共 ${total} 篇 · 更新于 ${shortDate(latest)}`;
    }
  }

  function setEmptyState(total) {
    if (total > 0) {
      els.emptyState.hidden = true;
      return;
    }
    els.emptyState.hidden = false;
    if (!state.articles.length) {
      els.emptyTitle.textContent = "镜像正在准备中";
      els.emptyText.textContent = "GitHub Actions 完成第一次同步后，推文会自动出现在这里。";
      els.emptyAction.textContent = "查看同步状态";
      els.emptyAction.href = "https://github.com/iCxin/scau-wechat-mirror/actions";
      els.emptyAction.target = "_blank";
      els.emptyAction.rel = "noopener";
      delete els.emptyAction.dataset.reset;
    } else {
      els.emptyTitle.textContent = "没有找到匹配的推文";
      els.emptyText.textContent = "试试更短的关键词，或者切换到「全部」标签。";
      els.emptyAction.textContent = "清除筛选";
      els.emptyAction.href = "#latest";
      els.emptyAction.removeAttribute("target");
      els.emptyAction.rel = "noopener";
      els.emptyAction.dataset.reset = "1";
    }
  }

  function wireCoverFallbacks() {
    document.querySelectorAll("img[data-cover]").forEach((img) => {
      if (img.dataset.wired === "1") return;
      img.dataset.wired = "1";
      img.addEventListener("error", () => {
        const host = img.closest(".card-cover, .featured-cover");
        if (host) host.innerHTML = '<div class="cover-placeholder" aria-hidden="true"></div>';
      }, { once: true });
    });
  }

  function render() {
    computeFiltered();
    const total = state.filtered.length;
    const showFeatured = state.sort === "new" && state.tag === "全部" && !state.query && total > 0;
    const featured = showFeatured ? state.filtered[0] : null;
    const gridList = showFeatured ? state.filtered.slice(1) : state.filtered;
    const visible = gridList.slice(0, state.page * PAGE_SIZE);

    els.featuredSlot.innerHTML = featured ? featuredMarkup(featured) : "";
    els.articleGrid.innerHTML = visible.map((article, index) => cardMarkup(article, index)).join("");
    els.loadMore.hidden = visible.length >= gridList.length || total === 0;

    setEmptyState(total);
    updateResultMeta(total);
    wireCoverFallbacks();
  }

  /* ------------------------------------------------------------------ */
  /* 阅读页                                                              */
  /* ------------------------------------------------------------------ */

  function parseHash() {
    const match = window.location.hash.match(/#article=([^&]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function sanitizeClient(html) {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    doc.querySelectorAll("script, style, iframe, object, embed, form, input, button, link, meta").forEach((node) => node.remove());
    doc.querySelectorAll("*").forEach((node) => {
      [...node.attributes].forEach((attr) => {
        const name = attr.name.toLowerCase();
        if (name.startsWith("on")) node.removeAttribute(attr.name);
        if (name === "style" || name === "class" || name === "id") node.removeAttribute(attr.name);
        if (name === "href" && /^\s*javascript:/i.test(attr.value || "")) node.removeAttribute(attr.name);
        if (name === "src" && /^\s*(javascript|data|blob):/i.test(attr.value || "")) node.removeAttribute(attr.name);
      });
    });
    doc.querySelectorAll("img").forEach((img) => {
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.decoding = "async";
      if (!img.getAttribute("alt")) img.setAttribute("alt", "");
    });
    doc.querySelectorAll("a[href]").forEach((link) => {
      if (/^https?:/i.test(link.getAttribute("href") || "")) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
    });
    return doc.body ? doc.body.innerHTML : "";
  }

  async function loadDetail(id) {
    const response = await fetch(`data/articles/${encodeURIComponent(id)}.json?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function addReaderLink(href, label, primary = false) {
    if (!href) return;
    const link = document.createElement("a");
    link.className = `reader-link${primary ? " primary" : ""}`;
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.innerHTML = `${esc(label)} ${ICONS.external}`;
    els.readerLinks.appendChild(link);
  }

  function renderReader(detail, fallback = {}) {
    const article = { ...fallback, ...(detail || {}) };
    els.readerTitle.textContent = article.title || fallback.title || "无标题";
    els.readerDate.textContent = fullDate(article.published_at || fallback.published_at);
    els.readerTags.innerHTML = tagMarkup(article.tags || fallback.tags || []);
    els.readerSource.textContent = article.source === "paper" ? "官方澎湃号同步" : "微信原文全文";

    const cover = article.cover || fallback.cover;
    if (cover) {
      els.readerCover.src = cover;
      els.readerCover.alt = article.title || "";
      els.readerCover.hidden = false;
      els.readerCover.onerror = () => { els.readerCover.hidden = true; };
    } else {
      els.readerCover.hidden = true;
      els.readerCover.removeAttribute("src");
    }

    if (article.content_html) {
      els.readerBody.innerHTML = sanitizeClient(article.content_html);
    } else {
      els.readerBody.innerHTML = `<p>${esc(article.summary || fallback.summary || "正文暂时没有同步到镜像，请通过下方原文链接阅读。")}</p>`;
    }

    els.readerLinks.innerHTML = "";
    addReaderLink(article.original_url || fallback.original_url, "微信原文", true);
    addReaderLink(article.paper_url || fallback.paper_url, "官方澎湃号");
    if (!els.readerLinks.children.length) {
      const note = document.createElement("span");
      note.className = "reader-link";
      note.textContent = "原文链接暂未同步";
      els.readerLinks.appendChild(note);
    }
    els.readerFooter.hidden = false;
    requestAnimationFrame(updateProgress);
  }

  function updateProgress() {
    const scroll = els.readerScroll;
    const max = scroll.scrollHeight - scroll.clientHeight;
    const ratio = max > 0 ? Math.min(1, Math.max(0, scroll.scrollTop / max)) : 0;
    els.readerProgress.style.width = `${ratio * 100}%`;
  }

  async function openReader(id, options = {}) {
    const { push = true } = options;
    if (!id) return;
    if (state.currentId === id && !els.reader.hidden) return;

    state.currentId = id;
    state.lastFocus = document.activeElement;
    const fallback = state.articles.find((article) => article.id === id) || {};

    els.reader.hidden = false;
    els.reader.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    els.readerTitle.textContent = fallback.title || "正在打开…";
    els.readerDate.textContent = fullDate(fallback.published_at);
    els.readerTags.innerHTML = tagMarkup(fallback.tags);
    els.readerSource.textContent = sourceLabel(fallback);
    els.readerCover.hidden = true;
    els.readerCover.removeAttribute("src");
    els.readerLinks.innerHTML = "";
    els.readerFooter.hidden = true;
    els.readerBody.innerHTML = '<div class="reader-skeleton"><span></span><span></span><span></span><span></span></div>';
    els.readerScroll.scrollTop = 0;
    els.readerProgress.style.width = "0%";

    if (push) {
      history.pushState({ article: id }, "", `#article=${encodeURIComponent(id)}`);
    }

    requestAnimationFrame(() => els.readerPanel.focus());

    try {
      const detail = await loadDetail(id);
      if (state.currentId !== id) return;
      renderReader(detail, fallback);
    } catch (error) {
      console.warn("读取文章正文失败", error);
      if (state.currentId !== id) return;
      els.readerBody.innerHTML = `<p>${esc(fallback.summary || "正文暂时没有同步到镜像。")}</p>`;
      els.readerLinks.innerHTML = "";
      addReaderLink(fallback.original_url, "微信原文", true);
      addReaderLink(fallback.paper_url, "官方澎湃号");
      els.readerFooter.hidden = false;
    }
  }

  function closeReader(options = {}) {
    const { push = true } = options;
    if (els.reader.hidden) return;
    els.reader.hidden = true;
    els.reader.setAttribute("aria-hidden", "true");
    els.readerBody.innerHTML = "";
    els.readerLinks.innerHTML = "";
    document.body.style.overflow = "";
    state.currentId = null;
    if (push) {
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
    if (state.lastFocus && typeof state.lastFocus.focus === "function") {
      state.lastFocus.focus();
    }
  }

  /* ------------------------------------------------------------------ */
  /* 事件                                                                */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    document.addEventListener("click", (event) => {
      const opener = event.target.closest("[data-open]");
      if (!opener) return;
      event.preventDefault();
      openReader(opener.dataset.open);
    });

    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-close]")) {
        closeReader();
      }
    });

    els.filterChips.addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      state.tag = chip.dataset.tag || "全部";
      state.page = 1;
      syncChips();
      render();
    });

    els.sortSelect.addEventListener("change", () => {
      state.sort = els.sortSelect.value;
      state.page = 1;
      render();
    });

    els.searchInput.addEventListener("input", debounce(() => {
      state.query = els.searchInput.value.trim();
      state.page = 1;
      render();
    }, 160));

    els.loadMore.addEventListener("click", () => {
      state.page += 1;
      render();
    });

    els.emptyAction.addEventListener("click", (event) => {
      if (!els.emptyAction.dataset.reset) return;
      event.preventDefault();
      state.query = "";
      state.tag = "全部";
      state.sort = "new";
      state.page = 1;
      els.searchInput.value = "";
      els.sortSelect.value = "new";
      syncChips();
      render();
      els.searchInput.focus();
    });

    els.themeToggle.addEventListener("click", () => {
      const dark = document.documentElement.getAttribute("data-theme") === "dark";
      const next = dark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("scau-theme", next); } catch (error) {}
      toast(dark ? "已切换到浅色主题" : "已切换到深色主题");
    });

    els.readerClose.addEventListener("click", () => closeReader());
    els.readerCopy.addEventListener("click", async () => {
      if (!state.currentId) return;
      const url = `${window.location.origin}${window.location.pathname}#article=${encodeURIComponent(state.currentId)}`;
      const ok = await copyText(url);
      toast(ok ? "文章链接已复制" : "复制失败，请手动复制地址栏");
    });

    els.readerScroll.addEventListener("scroll", updateProgress, { passive: true });

    els.reader.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closeReader();
        return;
      }
      if (event.key !== "Tab") return;
      const focusables = [...els.readerPanel.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')]
        .filter((node) => node.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (els.reader.hidden === false) return;
      const target = event.target;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT");
      if (event.key === "/" && !typing) {
        event.preventDefault();
        els.searchInput.focus();
      }
    });

    window.addEventListener("popstate", () => {
      const id = parseHash();
      if (id) openReader(id, { push: false });
      else closeReader({ push: false });
    });

    els.readerCover.addEventListener("error", () => { els.readerCover.hidden = true; });
  }

  /* ------------------------------------------------------------------ */
  /* 启动                                                                */
  /* ------------------------------------------------------------------ */

  function setLoadError() {
    els.metricTotal.textContent = "—";
    els.metricWechat.textContent = "—";
    els.metricUpdated.textContent = "—";
    els.resultMeta.textContent = "数据读取失败";
    els.syncText.textContent = "数据读取失败";
    els.syncPill.classList.add("is-error");
    els.footerSync.textContent = "等待首次同步";
    els.emptyState.hidden = false;
    els.emptyTitle.textContent = "镜像数据还没有生成";
    els.emptyText.textContent = "请先确认 GitHub Actions 已完成第一次同步，然后刷新页面。";
  }

  async function init() {
    bindEvents();
    els.footerYear.textContent = String(new Date().getFullYear());

    try {
      const response = await fetch(`data/index.json?v=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      state.data = data;
      state.articles = (Array.isArray(data.articles) ? data.articles : [])
        .filter((article) => article && article.id)
        .slice()
        .sort((a, b) => compareArticles(b, a));
      buildFilters();
      applyMeta();
      render();

      const hashId = parseHash();
      if (hashId) openReader(hashId, { push: false });
    } catch (error) {
      console.warn("镜像索引读取失败", error);
      state.data = null;
      state.articles = [];
      setLoadError();
    }
  }

  init();
})();
