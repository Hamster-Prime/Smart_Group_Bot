(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const app = document.getElementById("app");
  const content = document.getElementById("content");
  const desktopNav = document.getElementById("desktop-nav");
  const mobileNav = document.getElementById("mobile-nav");
  const pageTitle = document.getElementById("page-title");
  const pageSubtitle = document.getElementById("page-subtitle");
  const saveButton = document.getElementById("save-button");
  const reloadButton = document.getElementById("reload-button");
  const saveState = document.getElementById("save-state");
  const sidebarStatus = document.getElementById("sidebar-status");
  const sidebar = document.querySelector(".sidebar");
  const sidebarToggle = document.getElementById("sidebar-toggle");
  const toastRegion = document.getElementById("toast-region");
  const confirmDialog = document.getElementById("confirm-dialog");
  const confirmTitle = document.getElementById("confirm-title");
  const confirmMessage = document.getElementById("confirm-message");

  const ALL_NAV_ITEMS = [
    { id: "overview", label: "概览", icon: "layout-dashboard", subtitle: "运行状态与启动参数" },
    { id: "models", label: "模型", icon: "boxes", subtitle: "供应商、角色与回退链" },
    { id: "bot", label: "Bot", icon: "bot", subtitle: "消息、上下文与主动发言" },
    { id: "safety", label: "审核验证", icon: "shield-check", subtitle: "内容审核与入群验证" },
    { id: "media", label: "媒体能力", icon: "audio-waveform", subtitle: "语音、音乐、AV 与贴纸" },
    { id: "integrations", label: "外部服务", icon: "plug", subtitle: "Sub2API 接入" },
    { id: "logging", label: "日志", icon: "scroll-text", subtitle: "运行日志与文件轮转" },
    { id: "prompts", label: "Prompts", icon: "file-code-2", subtitle: "模型系统提示词" },
    { id: "groups", label: "群组", icon: "users", subtitle: "逐群行为覆盖" },
    { id: "access", label: "权限封禁", icon: "shield-ban", subtitle: "群授权、管理员与全局封禁" },
  ];

  const ROLE_META = {
    main: { label: "主模型", icon: "message-square", parent: "", embed: false },
    vision: { label: "视觉模型", icon: "scan-eye", parent: "继承主模型", embed: false },
    decision: { label: "决策模型", icon: "route", parent: "继承主模型", embed: false },
    moderation: { label: "审核模型", icon: "shield-alert", parent: "继承决策模型", embed: false },
    compress: { label: "压缩模型", icon: "minimize-2", parent: "继承主模型", embed: false },
    embed: { label: "向量模型", icon: "binary", parent: "继承主模型", embed: true },
  };

  const PROMPT_META = {
    decision: "回复决策",
    moderation: "内容审核",
    casual: "日常对话",
    manage_intent: "管理意图",
    compress: "上下文压缩",
    skill_tools: "技能工具",
    sticker_decision: "贴纸决策",
    reply_mode: "回复模式",
    persona: "角色人格",
    proactive_topic: "主动话题",
    style_distill: "风格提炼",
  };

  const GROUP_EDITABLE_KEYS = new Set([
    "av_enabled",
    "mute_all_replies",
    "at_reply_mode",
    "join_verification_enabled",
    "join_verification_provider",
    "welcome_message",
    "welcome_buttons",
    "default_permissions",
    "patrol_enabled",
    "raid_guard_enabled",
    "raid_guard_join_threshold",
    "raid_guard_window_seconds",
    "raid_guard_lockdown_seconds",
    "raid_guard_lookback_seconds",
    "raid_guard_challenge_timeout_seconds",
    "call_admin_enabled",
    "call_admin_targets",
    "vote_ban_enabled",
    "vote_ban_threshold",
    "vote_ban_duration_seconds",
    "vote_ban_trigger_limit",
    "vote_ban_trigger_window_seconds",
    "tts_mode",
    "proactive_enabled",
    "proactive_task_brief",
    "mimic_target_user_id",
    "mimic_target_user_name",
    "mimic_profile_text",
  ]);

  const RAID_GUARD_GROUP_INT_FIELDS = [
    { key: "raid_guard_join_threshold", label: "触发阈值（人数）", min: 2, max: 1000 },
    { key: "raid_guard_window_seconds", label: "检测窗口（秒）", min: 5, max: 3600 },
    { key: "raid_guard_lockdown_seconds", label: "锁定时长（秒）", min: 60, max: 86400 },
    { key: "raid_guard_lookback_seconds", label: "追溯窗口（秒）", min: 0, max: 86400 },
    { key: "raid_guard_challenge_timeout_seconds", label: "质询超时（秒）", min: 60, max: 86400 },
  ];

  const VOTE_BAN_GROUP_INT_FIELDS = [
    { key: "vote_ban_threshold", label: "封禁票数阈值", min: 2, max: 1000 },
    { key: "vote_ban_duration_seconds", label: "投票有效期（秒）", min: 60, max: 86400 },
    { key: "vote_ban_trigger_limit", label: "单用户触发上限", min: 1, max: 1000 },
    { key: "vote_ban_trigger_window_seconds", label: "触发统计窗口（秒）", min: 60, max: 604800 },
  ];

  const AUTO_DELETE_CATEGORY_META = [
    { key: "reply", label: "普通 AI 回复" },
    { key: "management", label: "命令与管理提示" },
    { key: "moderation", label: "审核通知" },
    { key: "media", label: "语音、音乐与贴纸" },
    { key: "proactive", label: "主动话题" },
    { key: "keyword", label: "关键词回复" },
    { key: "scheduled", label: "定时消息" },
    { key: "welcome", label: "入群欢迎" },
    { key: "call_admin", label: "呼叫管理员" },
    { key: "vote", label: "民主投票" },
  ];

  const PERMISSION_FIELD_FALLBACK = [
    ["can_send_messages", "发送文字消息"],
    ["can_send_audios", "发送音频"],
    ["can_send_documents", "发送文件"],
    ["can_send_photos", "发送图片"],
    ["can_send_videos", "发送视频"],
    ["can_send_video_notes", "发送视频消息"],
    ["can_send_voice_notes", "发送语音消息"],
    ["can_send_polls", "发送投票"],
    ["can_send_other_messages", "发送贴纸/动画/游戏"],
    ["can_add_web_page_previews", "添加链接预览"],
    ["can_react_to_messages", "添加消息反应"],
    ["can_edit_tag", "编辑成员标签"],
    ["can_change_info", "修改群组信息"],
    ["can_invite_users", "邀请用户"],
    ["can_pin_messages", "置顶消息"],
    ["can_manage_topics", "管理话题"],
  ].map(([key, label]) => ({ key, label }));

  const state = {
    session: null,
    activeTab: "overview",
    document: null,
    config: null,
    baseline: null,
    configuredSecrets: new Set(),
    secretChanges: {},
    groups: [],
    groupBaselines: new Map(),
    groupsError: "",
    loading: true,
    saving: false,
    groupSaving: new Set(),
    groupResources: new Map(),
    groupTelegramAdmins: new Map(),
    groupTemplateButtonDrafts: new Map(),
    resourceFormDrafts: new Map(),
    resourceFormBaselines: new Map(),
    permissionFields: PERMISSION_FIELD_FALLBACK,
    access: null,
    accessAdminGroup: null,
    promptKey: "decision",
    listPages: new Map(),
    groupSearch: "",
  };

  const LIST_PAGE_SIZE = 8;
  const RESOURCE_DRAFT_FORM_SELECTOR = [
    "[data-entry-edit-form]",
    "[data-rule-edit-form]",
    "[data-memory-edit-form]",
    "[data-resource-form]",
  ].join(", ");

  function paginate(items, key) {
    const total = items.length;
    const pages = Math.max(1, Math.ceil(total / LIST_PAGE_SIZE));
    const current = Math.min(Math.max(1, state.listPages.get(key) || 1), pages);
    const start = (current - 1) * LIST_PAGE_SIZE;
    return { slice: items.slice(start, start + LIST_PAGE_SIZE), current, pages, total };
  }

  function pagerMarkup(key, current, pages, total) {
    if (pages <= 1) return "";
    return `
      <div class="list-pager">
        <button class="mini-icon-button" type="button" data-action="list-page" data-list-key="${attr(key)}" data-page="${current - 1}"${current <= 1 ? " disabled" : ""} aria-label="上一页" title="上一页">${icon("chevron-left")}</button>
        <span>${current} / ${pages} 页 · 共 ${total} 条</span>
        <button class="mini-icon-button" type="button" data-action="list-page" data-list-key="${attr(key)}" data-page="${current + 1}"${current >= pages ? " disabled" : ""} aria-label="下一页" title="下一页">${icon("chevron-right")}</button>
      </div>`;
  }

  function paginatedRows(items, key, renderRow, emptyText = "暂无记录") {
    if (!items.length) return `<p class="field-hint">${escapeHtml(emptyText)}</p>`;
    const page = paginate(items, key);
    return page.slice.map(renderRow).join("") + pagerMarkup(key, page.current, page.pages, page.total);
  }

  function navItems() {
    return state.session?.can_manage_global
      ? ALL_NAV_ITEMS
      : ALL_NAV_ITEMS.filter(item => item.id === "groups");
  }

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.body = body;
    }
  }

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function attr(value) {
    return escapeHtml(value);
  }

  function getPath(object, path) {
    return String(path).split(".").reduce((current, key) => current?.[key], object);
  }

  function setPath(object, path, value) {
    const parts = String(path).split(".");
    const last = parts.pop();
    let current = object;
    for (const part of parts) {
      if (current[part] == null || typeof current[part] !== "object") current[part] = {};
      current = current[part];
    }
    current[last] = value;
  }

  function sameValue(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function configDirty() {
    if (!state.config || !state.baseline) return false;
    return !sameValue(state.config, state.baseline) || Object.keys(state.secretChanges).length > 0;
  }

  function groupDirty(group) {
    const buttonDraft = state.groupTemplateButtonDrafts.get(String(group.id));
    return Boolean(buttonDraft?.error)
      || !sameValue(group.settings, state.groupBaselines.get(String(group.id)));
  }

  function anyGroupDirty() {
    return state.groups.some(groupDirty)
      || [...state.groupTemplateButtonDrafts.values()].some(draft => Boolean(draft?.error));
  }

  function anyResourceFormDirty() {
    return state.resourceFormDrafts.size > 0;
  }

  function restartChanges() {
    const paths = state.document?.restart_required_paths || [];
    return paths.filter(path => !sameValue(getPath(state.config, path), getPath(state.baseline, path)));
  }

  function icon(name, className = "") {
    return `<i data-lucide="${attr(name)}"${className ? ` class="${attr(className)}"` : ""}></i>`;
  }

  function refreshIcons() {
    if (window.lucide?.createIcons) window.lucide.createIcons({ attrs: { "aria-hidden": "true" } });
  }

  function telegramInit() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      tg.disableVerticalSwipes?.();
    } catch (_) {
      // Older Telegram clients do not expose every WebApp method.
    }
  }

  function authHeaders(hasBody = false) {
    const headers = { Authorization: `tma ${tg?.initData || ""}` };
    if (hasBody) headers["Content-Type"] = "application/json";
    return headers;
  }

  function extractError(body, fallback) {
    const structuredError = body?.error && typeof body.error === "object" ? body.error : null;
    if (structuredError?.message) {
      const details = structuredError.details;
      if (typeof details === "string" && details.trim()) return `${structuredError.message}：${details}`;
      if (Array.isArray(details) && details.length) {
        const rendered = details.map(item => item?.message || item?.msg || String(item)).filter(Boolean).join("；");
        if (rendered) return `${structuredError.message}：${rendered}`;
      }
      return structuredError.message;
    }
    const detail = body?.detail ?? body?.error ?? body?.message;
    if (Array.isArray(detail)) {
      return detail
        .map(item => item?.msg || item?.message || String(item))
        .filter(Boolean)
        .join("；");
    }
    if (typeof detail === "string" && detail.trim()) return detail;
    return fallback;
  }

  async function apiFetch(url, options = {}) {
    const hasBody = options.body != null;
    const response = await fetch(url, {
      ...options,
      headers: { ...authHeaders(hasBody), ...(options.headers || {}) },
    });
    const text = await response.text();
    let body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (_) {
        body = { message: text };
      }
    }
    if (!response.ok) {
      throw new ApiError(extractError(body, `请求失败 (${response.status})`), response.status, body);
    }
    return body;
  }

  function showToast(message, type = "success", duration = 3600) {
    const toast = document.createElement("div");
    const iconName = type === "error" ? "circle-x" : type === "warning" ? "triangle-alert" : "circle-check";
    toast.className = `toast ${type}`;
    toast.innerHTML = `${icon(iconName)}<span>${escapeHtml(message)}</span>`;
    toastRegion.appendChild(toast);
    refreshIcons();
    window.setTimeout(() => toast.remove(), duration);
  }

  function askConfirmation(title, message) {
    if (!confirmDialog?.showModal) return Promise.resolve(window.confirm(message));
    confirmTitle.textContent = title;
    confirmMessage.textContent = message;
    confirmDialog.returnValue = "cancel";
    confirmDialog.showModal();
    refreshIcons();
    return new Promise(resolve => {
      confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "confirm"), { once: true });
    });
  }

  function navMarkup(item) {
    return `
      <button class="nav-button${state.activeTab === item.id ? " active" : ""}" type="button" data-nav="${item.id}" aria-current="${state.activeTab === item.id ? "page" : "false"}">
        ${icon(item.icon)}<span>${escapeHtml(item.label)}</span>
      </button>`;
  }

  function updateNavigation() {
    const items = navItems();
    const mobileScrollLeft = mobileNav.scrollLeft;
    desktopNav.innerHTML = items.map(navMarkup).join("");
    mobileNav.innerHTML = items.map(navMarkup).join("");
    mobileNav.scrollLeft = mobileScrollLeft;
    const active = items.find(item => item.id === state.activeTab) || items[0];
    pageTitle.textContent = active.label;
    pageSubtitle.textContent = active.subtitle;
  }

  function updateChrome() {
    updateNavigation();
    const dirty = configDirty();
    const restart = restartChanges();
    saveButton.hidden = !state.session?.can_manage_global;
    saveButton.disabled = !dirty || state.saving || state.loading;
    reloadButton.disabled = state.saving || state.loading || state.groupSaving.size > 0;
    saveButton.innerHTML = state.saving
      ? `<span class="spinner spinner-compact"></span><span>保存中</span>`
      : `${icon("save")}<span>保存配置</span>`;
    saveState.hidden = !state.session?.can_manage_global;
    saveState.className = `save-state${dirty ? " dirty" : ""}`;
    saveState.textContent = dirty ? (restart.length ? "有未保存更改，含重启项" : "有未保存更改") : `修订 ${state.document?.revision ?? "-"}`;
    const masterKey = state.document?.bootstrap?.master_key_configured;
    sidebarStatus.innerHTML = state.session?.can_manage_global ? `
      <div class="status-line"><span class="status-dot"></span><span>运行时配置 · r${escapeHtml(state.document?.revision ?? "-")}</span></div>
      <div class="status-line"><span class="status-dot${masterKey ? "" : " warning"}"></span><span>密钥加密 ${masterKey ? "已就绪" : "未配置"}</span></div>`
      : `<div class="status-line"><span class="status-dot"></span><span>群管理员模式</span></div>`;
    refreshIcons();
  }

  function pageHead(title, description, actions = "") {
    return `
      <div class="page-head">
        <div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(description)}</p></div>
        ${actions ? `<div class="page-actions">${actions}</div>` : ""}
      </div>`;
  }

  function sectionHead(title, description = "", action = "") {
    return `
      <div class="section-heading">
        <div><h3>${escapeHtml(title)}</h3>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div>
        ${action}
      </div>`;
  }

  function field(path, label, options = {}) {
    const value = getPath(state.config, path);
    const id = `field-${path.replaceAll(".", "-")}`;
    const full = options.full ? " full" : "";
    const kind = options.kind || (options.type === "number" ? "number" : "string");
    const restart = (state.document?.restart_required_paths || []).includes(path);
    const hint = options.hint || "";
    const labelRight = restart ? `<span class="badge warning">${icon("rotate-ccw")}需重启</span>` : "";
    const common = `id="${id}" data-path="${attr(path)}" data-kind="${attr(kind)}"${options.required ? " required" : ""}${options.disabled ? " disabled" : ""}`;
    let control;
    if (options.type === "select") {
      control = `<select ${common}>${(options.options || []).map(item => {
        const option = typeof item === "string" ? { value: item, label: item } : item;
        return `<option value="${attr(option.value)}"${String(value ?? "") === String(option.value) ? " selected" : ""}>${escapeHtml(option.label)}</option>`;
      }).join("")}</select>`;
    } else if (options.type === "textarea") {
      control = `<textarea ${common}${options.maxlength ? ` maxlength="${options.maxlength}"` : ""}${options.rows ? ` rows="${options.rows}"` : ""}>${escapeHtml(value ?? "")}</textarea>`;
    } else {
      const displayValue = kind === "array" ? (Array.isArray(value) ? value.join(", ") : "") : (value ?? "");
      const inputType = options.type === "url" ? "text" : (options.type || "text");
      control = `<input ${common} type="${attr(inputType)}" value="${attr(displayValue)}"${options.type === "url" ? " inputmode=\"url\"" : ""}${options.min != null ? ` min="${options.min}"` : ""}${options.max != null ? ` max="${options.max}"` : ""}${options.step != null ? ` step="${options.step}"` : ""}${options.maxlength ? ` maxlength="${options.maxlength}"` : ""}${options.placeholder ? ` placeholder="${attr(options.placeholder)}"` : ""}${options.list ? ` list="${attr(options.list)}"` : ""} autocomplete="off">`;
    }
    return `
      <div class="field${full}">
        <div class="field-label-row"><label class="field-label" for="${id}">${escapeHtml(label)}</label>${labelRight}</div>
        ${control}
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
  }

  function toggle(path, label, hint = "", full = false) {
    const value = Boolean(getPath(state.config, path));
    const id = `field-${path.replaceAll(".", "-")}`;
    const restart = (state.document?.restart_required_paths || []).includes(path);
    return `
      <div class="toggle-field${full ? " full" : ""}">
        <div class="toggle-copy">
          <strong>${escapeHtml(label)}${restart ? ` <span class="badge warning">需重启</span>` : ""}</strong>
          ${hint ? `<small>${escapeHtml(hint)}</small>` : ""}
        </div>
        <label class="toggle" for="${id}">
          <input id="${id}" type="checkbox" data-path="${attr(path)}" data-kind="boolean" aria-label="${attr(label)}"${value ? " checked" : ""}>
          <span class="toggle-track"></span>
        </label>
      </div>`;
  }

  function secretField(path, label, hint = "") {
    const change = state.secretChanges[path];
    const configured = state.configuredSecrets.has(path);
    const clearing = change?.action === "clear";
    const replacing = change?.action === "replace";
    const status = clearing
      ? `<span class="badge danger">${icon("trash-2")}保存后清除</span>`
      : replacing
        ? `<span class="badge warning">${icon("pencil")}等待替换</span>`
        : configured
          ? `<span class="badge success">${icon("key-round")}已配置</span>`
          : `<span class="badge">未配置</span>`;
    const action = clearing
      ? `<button class="mini-icon-button" type="button" data-action="secret-undo" data-secret-path="${attr(path)}" aria-label="撤销清除" title="撤销清除">${icon("undo-2")}</button>`
      : (configured || replacing)
        ? `<button class="mini-icon-button danger" type="button" data-action="secret-clear" data-secret-path="${attr(path)}" aria-label="清除密钥" title="清除密钥">${icon("trash-2")}</button>`
        : "";
    return `
      <div class="field">
        <label class="field-label">${escapeHtml(label)}</label>
        <div class="secret-control">
          <input type="password" data-secret-input="${attr(path)}" value="${attr(replacing ? change.value || "" : "")}" placeholder="${configured ? "留空保留当前密钥" : "输入新密钥"}"${clearing ? " disabled" : ""} autocomplete="new-password">
          <div class="secret-status">${status}${action}</div>
        </div>
        ${hint ? `<span class="field-hint">${escapeHtml(hint)}</span>` : ""}
      </div>`;
  }

  function providerOptions(selected, allowInherit = true, inheritLabel = "继承主模型") {
    const providers = state.config.models.providers || [];
    const options = [];
    if (allowInherit) options.push({ value: "", label: inheritLabel });
    for (const provider of providers) options.push({ value: provider.name, label: provider.name });
    return options.map(item => `<option value="${attr(item.value)}"${String(selected ?? "") === item.value ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
  }

  function renderOverview() {
    const bootstrap = state.document.bootstrap || {};
    const restartPaths = state.document.restart_required_paths || [];
    const pathLabels = {
      "bot.parse_mode": "消息解析格式",
    };
    return `
      ${pageHead("运行概览", "数据库中的配置会立即应用；启动参数保持只读。")}
      <div class="section-stack">
        <section class="settings-section">
          <div class="overview-grid">
            <div class="metric"><span>配置修订</span><strong>r${escapeHtml(state.document.revision)}</strong></div>
            <div class="metric"><span>模型供应商</span><strong>${state.config.models.providers.length}</strong></div>
            <div class="metric"><span>已配置密钥</span><strong>${state.configuredSecrets.size}</strong></div>
            <div class="metric"><span>管理群组</span><strong>${state.groupsError ? "-" : state.groups.length}</strong></div>
          </div>
        </section>

        ${!bootstrap.master_key_configured ? `
          <div class="notice">${icon("triangle-alert")}<span>CONFIG_MASTER_KEY 尚未配置。普通设置仍可保存，但新增或替换密钥会被拒绝。</span></div>` : ""}

        <section class="settings-section">
          ${sectionHead("启动参数", "这些项目由部署环境提供，修改后需要重启进程。")}
          <dl class="bootstrap-table">
            <div class="bootstrap-row"><dt>Mini App 公网地址</dt><dd>${escapeHtml(bootstrap.public_base_url || "未配置")}</dd></div>
            <div class="bootstrap-row"><dt>监听地址</dt><dd>${escapeHtml(bootstrap.listen_host || "-")}:${escapeHtml(bootstrap.listen_port ?? "-")}</dd></div>
            <div class="bootstrap-row"><dt>数据库</dt><dd>${escapeHtml(bootstrap.database_url || "-")}</dd></div>
            <div class="bootstrap-row"><dt>配置加密主密钥</dt><dd>${bootstrap.master_key_configured ? `<span class="badge success">已配置</span>` : `<span class="badge warning">未配置</span>`}</dd></div>
          </dl>
        </section>

        <section class="settings-section">
          ${sectionHead("重启项", "其余运行时设置保存后无需重启。")}
          <div class="item-list">
            ${restartPaths.map(path => `
              <div class="notice info">${icon("rotate-ccw")}<span><strong>${escapeHtml(pathLabels[path] || path)}</strong><br>${escapeHtml(path)}</span></div>`).join("") || `<div class="empty-state empty-state-compact">${icon("circle-check")}<p>当前没有重启项</p></div>`}
          </div>
        </section>
      </div>`;
  }

  function renderProviderCard(provider, index) {
    const secretPath = `providers.${String(provider.name || "").trim().toLowerCase()}.api_key`;
    return `
      <article class="item-card">
        <div class="item-card-header">
          <div class="item-card-title">${icon("server")}<strong>${escapeHtml(provider.name || `供应商 ${index + 1}`)}</strong></div>
          <div class="item-actions">
            <button class="mini-icon-button danger" type="button" data-action="remove-provider" data-index="${index}" aria-label="删除供应商" title="删除供应商">${icon("trash-2")}</button>
          </div>
        </div>
        <div class="item-card-body">
          <div class="field-grid">
            <div class="field">
              <label class="field-label" for="provider-name-${index}">配置名称</label>
              <input id="provider-name-${index}" type="text" value="${attr(provider.name)}" data-path="models.providers.${index}.name" data-kind="string" data-provider-index="${index}" data-provider-old-name="${attr(provider.name)}" maxlength="64" pattern="[A-Za-z0-9_-]+" required autocomplete="off">
              <span class="field-hint">模型角色通过此名称引用</span>
            </div>
            <div class="field">
              <label class="field-label" for="provider-type-${index}">协议供应商</label>
              <input id="provider-type-${index}" type="text" value="${attr(provider.provider)}" data-path="models.providers.${index}.provider" data-kind="lower-string" list="provider-types" maxlength="64" required autocomplete="off">
            </div>
            <div class="field full">
              <label class="field-label" for="provider-base-${index}">API Base URL</label>
              <input id="provider-base-${index}" type="text" inputmode="url" value="${attr(provider.api_base)}" data-path="models.providers.${index}.api_base" data-kind="string" placeholder="使用供应商默认地址" autocomplete="off">
            </div>
            <div class="field">
              <label class="field-label" for="provider-endpoint-${index}">聊天接口</label>
              <select id="provider-endpoint-${index}" data-path="models.providers.${index}.chat_endpoint" data-kind="string">
                <option value="auto"${provider.chat_endpoint === "auto" ? " selected" : ""}>自动识别</option>
                <option value="chat_completions"${provider.chat_endpoint === "chat_completions" ? " selected" : ""}>Chat Completions</option>
                <option value="responses"${provider.chat_endpoint === "responses" ? " selected" : ""}>Responses</option>
              </select>
            </div>
            ${secretField(secretPath, "API Key", "空白不会覆盖已保存的密钥")}
            ${toggle(`models.providers.${index}.stream`, "启用流式请求", "供应商连接使用流式返回")}
          </div>
        </div>
      </article>`;
  }

  function renderFallback(roleName, item, index) {
    return `
      <div class="fallback-row">
        <div class="field">
          <label class="field-label" for="fallback-provider-${roleName}-${index}">供应商</label>
          <select id="fallback-provider-${roleName}-${index}" data-path="models.${roleName}.fallbacks.${index}.provider" data-kind="string" required>
            ${providerOptions(item.provider, false)}
          </select>
        </div>
        <div class="field">
          <label class="field-label" for="fallback-model-${roleName}-${index}">模型</label>
          <input id="fallback-model-${roleName}-${index}" type="text" value="${attr(item.model)}" data-path="models.${roleName}.fallbacks.${index}.model" data-kind="string" maxlength="255" placeholder="留空沿用当前模型" autocomplete="off">
        </div>
        <button class="mini-icon-button danger" type="button" data-action="remove-fallback" data-role="${roleName}" data-index="${index}" aria-label="删除回退模型" title="删除回退模型">${icon("trash-2")}</button>
      </div>`;
  }

  function renderRoleCard(roleName) {
    const meta = ROLE_META[roleName];
    const role = state.config.models[roleName];
    const allowInherit = roleName !== "main";
    return `
      <article class="item-card">
        <div class="item-card-header">
          <div class="item-card-title">${icon(meta.icon)}<strong>${escapeHtml(meta.label)}</strong></div>
          ${allowInherit ? `<span class="badge info">支持继承</span>` : `<span class="badge success">主路由</span>`}
        </div>
        <div class="item-card-body">
          <div class="field-grid three">
            <div class="field">
              <label class="field-label" for="role-provider-${roleName}">供应商</label>
              <select id="role-provider-${roleName}" data-path="models.${roleName}.provider" data-kind="string"${allowInherit ? "" : " required"}>
                ${providerOptions(role.provider, allowInherit, meta.parent)}
              </select>
            </div>
            <div class="field">
              <label class="field-label" for="role-model-${roleName}">模型名称</label>
              <input id="role-model-${roleName}" type="text" value="${attr(role.model)}" data-path="models.${roleName}.model" data-kind="string" maxlength="255"${roleName === "main" || meta.embed ? " required" : ""} placeholder="${allowInherit && !meta.embed ? "留空继承上级模型" : "模型标识"}" autocomplete="off">
            </div>
            ${field(`models.${roleName}.timeout_sec`, "超时（秒）", { type: "number", min: 1, max: 600, step: 0.1, required: true })}
            ${meta.embed ? "" : field(`models.${roleName}.temperature`, "Temperature", { type: "number", min: 0, max: 2, step: 0.05, required: true })}
            ${meta.embed ? "" : field(`models.${roleName}.max_tokens`, "最大输出 Token", { type: "number", min: 1, max: 2000000, step: 1, required: true })}
            ${meta.embed ? "" : field(`models.${roleName}.reasoning_effort`, "推理强度", {
              type: "select",
              options: [
                { value: "", label: "不发送参数" },
                { value: "none", label: "none" },
                { value: "minimal", label: "minimal" },
                { value: "low", label: "low" },
                { value: "medium", label: "medium" },
                { value: "high", label: "high" },
              ],
            })}
          </div>
          <div class="subsection">
            <div class="subsection-head">
              <strong>回退链</strong>
              <button class="text-button" type="button" data-action="add-fallback" data-role="${roleName}">${icon("plus")}添加回退</button>
            </div>
            <div class="fallback-list">
              ${(role.fallbacks || []).map((item, index) => renderFallback(roleName, item, index)).join("") || `<span class="field-hint">未设置回退模型</span>`}
            </div>
          </div>
        </div>
      </article>`;
  }

  function renderModels() {
    return `
      ${pageHead("模型路由", "管理 API 供应商、任务角色和失败回退顺序。")}
      <datalist id="provider-types">
        <option value="gemini"></option><option value="openai"></option><option value="anthropic"></option><option value="openrouter"></option>
        <option value="openai_compatible"></option><option value="minimax"></option><option value="deepseek"></option><option value="moonshot"></option>
        <option value="dashscope"></option><option value="volcengine"></option><option value="xai"></option><option value="mistral"></option><option value="groq"></option>
      </datalist>
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("供应商", "配置名称必须唯一，且只能使用字母、数字、下划线和连字符。", `<button class="secondary-button" type="button" data-action="add-provider">${icon("plus")}添加供应商</button>`)}
          <div class="item-list">${state.config.models.providers.map(renderProviderCard).join("")}</div>
        </section>
        <section class="settings-section">
          ${sectionHead("模型角色", "空白的非主模型字段会沿用上级模型。")}
          <div class="item-list">${Object.keys(ROLE_META).map(renderRoleCard).join("")}</div>
        </section>
        <section class="settings-section">
          ${sectionHead("重试策略", "所有模型角色共用。")}
          <div class="field-grid three">
            ${field("models.retry_attempts", "尝试次数", { type: "number", min: 1, max: 10, step: 1, required: true })}
            ${field("models.retry_backoff_sec", "退避时间（秒）", { type: "number", min: 0, max: 60, step: 0.1, required: true })}
            ${field("models.retry_timeout_multiplier", "超时倍率", { type: "number", min: 1, max: 10, step: 0.05, required: true })}
          </div>
        </section>
      </div>`;
  }

  function renderBot() {
    const deleteCategories = new Set(state.config.bot.auto_delete_categories || []);
    const categorySeconds = state.config.bot.auto_delete_category_seconds || {};
    const categoryModes = state.config.bot.auto_delete_category_mode || {};
    const categoryRow = (value, label) => {
      const enabled = deleteCategories.has(value);
      const seconds = categorySeconds[value];
      const mode = categoryModes[value] === "button" ? "button" : "timer";
      return `
      <div class="delete-category-row">
        <label class="choice-row">
          <input type="checkbox" data-auto-delete-category="${value}"${enabled ? " checked" : ""}>
          <span>${escapeHtml(label)}</span>
        </label>
        <select class="delete-category-mode" data-auto-delete-mode="${value}" aria-label="${attr(label)}清理方式" title="定时自动删除或提供删除按钮（二选一）"${enabled ? "" : " disabled"}>
          <option value="timer"${mode === "timer" ? " selected" : ""}>自动删除</option>
          <option value="button"${mode === "button" ? " selected" : ""}>删除按钮</option>
        </select>
        <input class="delete-category-seconds" type="number" min="0" max="604800" step="1" data-auto-delete-seconds="${value}" value="${attr(seconds == null ? "" : seconds)}" placeholder="默认" aria-label="${attr(label)}自动删除秒数" title="留空使用上方全局秒数"${enabled && mode === "timer" ? "" : " disabled"}>
      </div>`;
    };
    return `
      ${pageHead("Bot 行为", "调整消息处理、上下文预算与主动发言节奏。")}
      <datalist id="parse-modes"><option value="HTML"></option><option value="Markdown"></option><option value="MarkdownV2"></option></datalist>
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("消息处理")}
          <div class="field-grid three">
            ${field("bot.parse_mode", "消息解析格式", { maxlength: 32, list: "parse-modes", placeholder: "留空发送纯文本" })}
            ${field("bot.inbound_debounce_seconds", "入站合并窗口（秒）", { type: "number", min: 0, max: 60, step: 0.1, required: true })}
            ${field("bot.reply_batch_timeout_seconds", "单次回复总时限（秒）", { type: "number", min: 5, max: 120, step: 1, required: true, hint: "覆盖决策、工具、生成和投递的总预算" })}
            ${field("bot.auto_delete_seconds", "自动删除（秒）", { type: "number", min: 0, max: 604800, step: 1, required: true, hint: "0 表示不自动删除；作为各类别的默认秒数" })}
            ${toggle("bot.enable_typing", "显示输入状态", "生成回复时发送 typing 状态")}
            ${toggle("bot.enable_streaming", "流式编辑消息", "生成期间持续更新 Telegram 消息")}
            ${field("bot.stream_chunk_size", "流式首段字符数", { type: "number", min: 8, max: 4096, step: 1, required: true })}
            ${field("bot.stream_edit_interval_sec", "编辑间隔（秒）", { type: "number", min: 0.3, max: 30, step: 0.1, required: true })}
          </div>
          <div class="choice-grid full-width-control">
            ${AUTO_DELETE_CATEGORY_META.map(({ key, label }) => categoryRow(key, label)).join("")}
          </div>
          <p class="field-hint">勾选的类别按所选方式清理：「自动删除」按右侧秒数定时删除（留空用全局秒数）；「删除按钮」在消息下方提供管理员可用的内联删除按钮，两者互斥。</p>
        </section>
        <section class="settings-section">
          ${sectionHead("上下文预算")}
          <div class="field-grid three">
            ${field("bot.decision_context_items", "决策上下文条数", { type: "number", min: 0, max: 20, step: 1, required: true })}
            ${field("bot.max_context_tokens", "最大上下文 Token", { type: "number", min: 1024, max: 2000000, step: 1, required: true })}
            ${field("bot.max_output_tokens", "全局最大输出 Token", { type: "number", min: 256, max: 2000000, step: 1, required: true })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("主动发言", "群组页可逐群开关并设置任务简述。")}
          <div class="field-grid three">
            ${toggle("bot.proactive_default_enabled", "新群默认启用", "作为群级设置的默认策略")}
            ${field("bot.proactive_idle_minutes", "空闲触发（分钟）", { type: "number", min: 180, max: 43200, step: 1, required: true })}
            ${field("bot.proactive_jitter_minutes", "随机抖动（分钟）", { type: "number", min: 0, max: 1440, step: 1, required: true })}
            ${field("bot.proactive_check_interval_seconds", "检查间隔（秒）", { type: "number", min: 15, max: 3600, step: 1, required: true })}
            ${field("bot.proactive_quiet_hours_start", "安静时段开始", { type: "number", min: 0, max: 23, step: 1, required: true })}
            ${field("bot.proactive_quiet_hours_end", "安静时段结束", { type: "number", min: 0, max: 23, step: 1, required: true })}
            ${field("bot.proactive_retry_minutes", "失败重试（分钟）", { type: "number", min: 5, max: 1440, step: 1, required: true })}
          </div>
        </section>
      </div>`;
  }

  function renderSafety() {
    const provider = state.config.verification.provider || "turnstile";
    return `
      ${pageHead("审核与验证", "管理违规判定、低置信度质询和新成员验证。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("内容审核")}
          <div class="field-grid three">
            ${toggle("moderation.enabled", "启用内容审核", "对群消息执行审核规则")}
            ${field("moderation.warn_threshold", "警告阈值", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("moderation.high_confidence_threshold", "高置信度阈值", { type: "number", min: 0, max: 1, step: 0.01, required: true })}
            ${field("moderation.challenge_timeout_seconds", "质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true })}
            ${toggle("moderation.bot_screening_enabled", "审核其他 bot 消息", "guest 模式等 bot 消息先审核，累计干净消息达标后加入白名单")}
            ${field("moderation.bot_screening_message_count", "bot 白名单所需干净消息数", { type: "number", min: 1, max: 100, step: 1, required: true })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("入群验证默认值", "未单独配置的群组使用这里的开关和验证服务；群组管理员可在群组页覆盖。")}
          <div class="field-grid three">
            ${toggle("verification.enabled", "默认启用新成员验证", "通过当前选择的人机验证服务后恢复群权限")}
            ${field("verification.timeout_seconds", "验证超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true })}
            ${field("verification.check_interval_seconds", "状态检查间隔（秒）", { type: "number", min: 5, max: 3600, step: 0.1, required: true })}
            ${field("verification.provider", "默认验证服务", { type: "select", options: [
              { value: "turnstile", label: "Cloudflare Turnstile" },
              { value: "hcaptcha", label: "hCaptcha" },
              { value: "turnstile_hcaptcha", label: "Turnstile + hCaptcha（双重验证）" },
            ] })}
            ${field("verification.turnstile_site_key", "Turnstile Site Key", { maxlength: 255 })}
            ${secretField("verification.turnstile_secret_key", "Turnstile Secret Key", "必须填写 Cloudflare 控制台中的 Secret Key，不能重复 Site Key；空白不会覆盖已保存值")}
            ${field("verification.hcaptcha_site_key", "hCaptcha Site Key", { maxlength: 255 })}
            ${secretField("verification.hcaptcha_secret_key", "hCaptcha Secret Key", "空白不会覆盖已保存值")}
          </div>
          <div class="notice info">${icon("refresh-cw")}<span>默认使用 ${provider === "turnstile_hcaptcha" ? "Turnstile + hCaptcha 双重验证（需依次通过两项，且两套密钥都必须配置）" : provider === "hcaptcha" ? "hCaptcha" : "Cloudflare Turnstile"}；两套密钥可同时保留并随时切换，群组可单独选择服务。</span></div>
        </section>
        <section class="settings-section">
          ${sectionHead("自动巡检", "按时批量复查已知成员的名字和简介是否违反群规；违规者禁言并发起真人质询，通过恢复权限，超时移出群聊（不封禁）。群组页可逐群覆盖开关和手动触发。")}
          <div class="field-grid three">
            ${toggle("patrol.enabled", "默认启用自动巡检", "需要已配置真人验证服务；群组管理员可在群组页覆盖")}
            ${field("patrol.schedule_time", "每日巡检时间", { maxlength: 5, required: true, placeholder: "04:30", hint: "24 小时制 HH:MM（Asia/Shanghai）" })}
            ${field("patrol.batch_size", "分批大小", { type: "number", min: 10, max: 5000, step: 1, required: true, hint: "每批检查的成员数，默认 500" })}
            ${field("patrol.batch_pause_seconds", "批间停顿（秒）", { type: "number", min: 0, max: 600, step: 0.5, required: true })}
            ${field("patrol.challenge_timeout_seconds", "质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未完成质询将被移出群聊（可重新加入）" })}
            ${field("patrol.check_interval_seconds", "调度检查间隔（秒）", { type: "number", min: 15, max: 3600, step: 1, required: true })}
            ${toggle("patrol.fetch_bio", "巡检时抓取简介", "逐个调用 getChat 获取简介，更全面但更慢")}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("呼叫管理员", "群成员发送 @admin 时 @ 群管理员，用于举报或紧急呼叫；群组页可逐群覆盖开关并选择要 @ 的管理员（默认全部）。")}
          <div class="field-grid three">
            ${toggle("call_admin.enabled", "默认启用呼叫管理员", "群组管理员可在群组页覆盖")}
            ${field("call_admin.cooldown_seconds", "呼叫冷却（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "同群两次 @admin 之间的最小间隔，防刷屏" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("民主投票封禁", "群成员可回复消息发送 /voteban，或明确要求 Bot 调用技能发起投票；两个入口共享持久化的单用户次数额度。票数达标即在本群封禁，管理员与最高管理员不可被投票。群组页可逐群覆盖各项。")}
          <div class="field-grid three">
            ${toggle("vote_ban.enabled", "默认启用民主投票封禁", "群组管理员可在群组页覆盖")}
            ${field("vote_ban.vote_threshold", "封禁票数阈值", { type: "number", min: 2, max: 1000, step: 1, required: true, hint: "含发起人自动投出的第一票" })}
            ${field("vote_ban.duration_seconds", "投票有效期（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未达票数的投票自动失效" })}
            ${field("vote_ban.trigger_limit", "单用户触发上限", { type: "number", min: 1, max: 1000, step: 1, required: true, hint: "命令和 AI 技能共用同一额度" })}
            ${field("vote_ban.trigger_window_seconds", "触发统计窗口（秒）", { type: "number", min: 60, max: 604800, step: 1, required: true, hint: "默认 3600 秒内最多触发 3 次；重启后额度仍保留" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("爆破防护", "短时间内大量成员加入时自动锁群：锁定期内任何新加入的成员都被移出（不封禁），并只发送一条防护提示；同时追溯锁定前进群的成员，统一 @ 要求真人质询，超时未通过将被移出（可重新加入）。群组页可逐群覆盖开关和各项阈值。")}
          <div class="field-grid three">
            ${toggle("raid_guard.enabled", "默认启用爆破防护", "需要已配置真人验证服务用于追溯质询；群组管理员可在群组页覆盖")}
            ${field("raid_guard.join_threshold", "触发阈值（人数）", { type: "number", min: 2, max: 1000, step: 1, required: true, hint: "检测窗口内加入人数达到该值即触发锁定" })}
            ${field("raid_guard.window_seconds", "检测窗口（秒）", { type: "number", min: 5, max: 3600, step: 1, required: true })}
            ${field("raid_guard.lockdown_seconds", "锁定时长（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "锁定期内的新加入会刷新锁定时间" })}
            ${field("raid_guard.lookback_seconds", "追溯窗口（秒）", { type: "number", min: 0, max: 86400, step: 1, required: true, hint: "触发前该时间段内进群的成员将被要求真人质询" })}
            ${field("raid_guard.challenge_timeout_seconds", "质询超时（秒）", { type: "number", min: 60, max: 86400, step: 1, required: true, hint: "超时未完成质询将被移出群聊（可重新加入）" })}
          </div>
        </section>
      </div>`;
  }

  function renderMedia() {
    return `
      ${pageHead("媒体能力", "配置语音合成、音乐检索、AV 检索和贴纸回退。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("豆包 TTS")}
          <div class="field-grid three">
            ${toggle("tts.enabled", "启用语音合成", "群级语音模式在群组页设置")}
            ${field("tts.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("tts.max_text_length", "最大文本长度", { type: "number", min: 1, max: 10000, step: 1, required: true })}
            ${field("tts.api_base", "API Base URL", { type: "url", maxlength: 1000, full: true })}
            ${field("tts.app_id", "App ID", { maxlength: 255 })}
            ${secretField("tts.app_key", "App Key", "空白不会覆盖已保存的密钥")}
            ${secretField("tts.access_key", "Access Key", "空白不会覆盖已保存的密钥")}
            ${field("tts.resource_id", "Resource ID", { maxlength: 255 })}
            ${field("tts.model", "模型", { maxlength: 255, placeholder: "使用服务默认模型" })}
            ${field("tts.speaker", "音色", { maxlength: 255, placeholder: "使用服务默认音色" })}
            ${field("tts.audio_format", "音频格式", { maxlength: 64 })}
            ${field("tts.sample_rate", "采样率", { type: "number", min: 8000, max: 192000, step: 1, required: true })}
            ${field("tts.bit_rate", "比特率", { type: "number", min: 8000, max: 512000, step: 1, required: true })}
            ${field("tts.emotion", "情感", { maxlength: 64, placeholder: "不指定" })}
            ${field("tts.emotion_scale", "情感强度", { type: "number", min: 1, max: 5, step: 1, required: true })}
            ${field("tts.speech_rate", "语速调整", { type: "number", min: -100, max: 100, step: 1, required: true })}
            ${field("tts.loudness_rate", "音量调整", { type: "number", min: -100, max: 100, step: 1, required: true })}
            ${field("tts.silence_duration_ms", "尾部静音（毫秒）", { type: "number", min: 0, max: 10000, step: 1, required: true })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("音乐检索")}
          <div class="field-grid three">
            ${toggle("music.enabled", "启用音乐检索", "允许音乐搜索与发送")}
            ${field("music.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("music.default_source", "默认音源", { maxlength: 64 })}
            ${field("music.base_url", "API 地址", { type: "url", maxlength: 1000, full: true })}
            ${field("music.stable_sources", "稳定音源", { kind: "array", full: true, hint: "多个音源使用逗号分隔" })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("AV 检索")}
          <div class="field-grid three">
            ${toggle("av.enabled", "启用 AV 检索", "群级开关可进一步限制")}
            ${field("av.http_timeout_sec", "请求超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("av.max_results", "最大结果数", { type: "number", min: 1, max: 100, step: 1, required: true })}
            ${field("av.javbus_base_url", "JavBus 地址", { type: "url", maxlength: 1000 })}
            ${field("av.madouqu_base_url", "Madouqu 地址", { type: "url", maxlength: 1000 })}
            ${field("av.dmm_base_url", "DMM 地址", { type: "url", maxlength: 1000 })}
            ${field("av.fc2_base_url", "FC2 地址", { type: "url", maxlength: 1000 })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("贴纸回退")}
          <div class="field-grid single">
            ${field("stickers.fallback_file_ids", "Telegram Sticker File IDs", { kind: "array", full: true, hint: "多个 File ID 使用逗号分隔" })}
          </div>
        </section>
      </div>`;
  }

  function renderIntegrations() {
    return `
      ${pageHead("外部服务", "管理第三方业务服务的连接与超时。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("Sub2API")}
          <div class="field-grid three">
            ${toggle("sub2api.enabled", "启用 Sub2API", "允许订阅查询技能访问服务")}
            ${field("sub2api.http_timeout_sec", "HTTP 超时（秒）", { type: "number", min: 1, max: 300, step: 0.1, required: true })}
            ${field("sub2api.check_timeout_sec", "检查超时（秒）", { type: "number", min: 1, max: 600, step: 0.1, required: true })}
            ${field("sub2api.base_url", "Base URL", { type: "url", maxlength: 1000, full: true })}
            ${secretField("sub2api.api_key", "API Key", "空白不会覆盖已保存的密钥")}
          </div>
        </section>
      </div>`;
  }

  function renderLogging() {
    const levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
    return `
      ${pageHead("日志", "日志级别与文件轮转策略在保存后立即生效。")}
      <div class="section-stack">
        <section class="settings-section">
          ${sectionHead("日志级别")}
          <div class="field-grid three">
            ${field("logging.level", "应用日志级别", { type: "select", options: levels })}
            ${field("logging.third_party_level", "第三方日志级别", { type: "select", options: levels })}
            ${field("logging.color", "终端颜色", { type: "select", options: [
              { value: "on", label: "开启" }, { value: "off", label: "关闭" }, { value: "auto", label: "自动" },
            ] })}
          </div>
        </section>
        <section class="settings-section">
          ${sectionHead("文件输出")}
          <div class="field-grid three">
            ${toggle("logging.to_file", "写入日志文件", "同时保留标准输出")}
            ${field("logging.file_path", "文件路径", { maxlength: 1000 })}
            ${field("logging.file_max_bytes", "单文件最大字节", { type: "number", min: 1024, max: 10737418240, step: 1, required: true })}
            ${field("logging.file_backup_count", "保留文件数", { type: "number", min: 1, max: 100, step: 1, required: true })}
          </div>
        </section>
      </div>`;
  }

  function promptDirty(key) {
    return getPath(state.config, `prompts.${key}`) !== getPath(state.baseline, `prompts.${key}`);
  }

  function renderPrompts() {
    const key = state.promptKey;
    const value = state.config.prompts[key] || "";
    return `
      ${pageHead("系统 Prompts", "编辑各模型任务使用的系统提示词；空值会使用项目内置默认。")}
      <div class="prompt-layout">
        <nav class="prompt-list" aria-label="Prompt 列表">
          ${Object.entries(PROMPT_META).map(([promptKey, label]) => `
            <button class="prompt-button${key === promptKey ? " active" : ""}" type="button" data-action="select-prompt" data-prompt-key="${promptKey}">
              <span>${escapeHtml(label)}</span>${promptDirty(promptKey) ? `<span class="prompt-dot" aria-label="已修改"></span>` : ""}
            </button>`).join("")}
        </nav>
        <section class="prompt-editor">
          <div class="prompt-editor-head">
            <div><h3>${escapeHtml(PROMPT_META[key])}</h3><span class="prompt-meta" id="prompt-meta">${value.length.toLocaleString("zh-CN")} 字符</span></div>
            <button class="mini-icon-button danger" type="button" data-action="clear-prompt" data-prompt-key="${key}" aria-label="清空并使用内置默认" title="清空并使用内置默认">${icon("eraser")}</button>
          </div>
          <textarea data-prompt-input="${key}" spellcheck="false">${escapeHtml(value)}</textarea>
        </section>
      </div>`;
  }

  function normalizeGroupSettings(settings = {}) {
    return {
      av_enabled: settings.av_enabled ?? false,
      mute_all_replies: settings.mute_all_replies ?? false,
      at_reply_mode: settings.at_reply_mode ?? false,
      join_verification_enabled: settings.join_verification_enabled == null ? null : Boolean(settings.join_verification_enabled),
      join_verification_provider: ["turnstile", "hcaptcha", "turnstile_hcaptcha"].includes(settings.join_verification_provider)
        ? settings.join_verification_provider
        : null,
      welcome_message: String(settings.welcome_message || ""),
      welcome_buttons: normalizeTemplateButtons(settings.welcome_buttons),
      default_permissions: normalizeDefaultPermissions(settings.default_permissions),
      patrol_enabled: settings.patrol_enabled == null ? null : Boolean(settings.patrol_enabled),
      raid_guard_enabled: settings.raid_guard_enabled == null ? null : Boolean(settings.raid_guard_enabled),
      ...Object.fromEntries(RAID_GUARD_GROUP_INT_FIELDS.map(({ key }) => [
        key,
        settings[key] == null ? null : Number(settings[key]),
      ])),
      call_admin_enabled: settings.call_admin_enabled == null ? null : Boolean(settings.call_admin_enabled),
      call_admin_targets: Array.isArray(settings.call_admin_targets)
        ? settings.call_admin_targets.map(Number).filter(value => Number.isFinite(value) && value > 0).sort((a, b) => a - b)
        : [],
      vote_ban_enabled: settings.vote_ban_enabled == null ? null : Boolean(settings.vote_ban_enabled),
      ...Object.fromEntries(VOTE_BAN_GROUP_INT_FIELDS.map(({ key }) => [
        key,
        settings[key] == null ? null : Number(settings[key]),
      ])),
      tts_mode: ["off", "on", "always"].includes(settings.tts_mode) ? settings.tts_mode : "off",
      proactive_enabled: settings.proactive_enabled == null ? null : Boolean(settings.proactive_enabled),
      proactive_task_brief: String(settings.proactive_task_brief || ""),
      mimic_target_user_id: Number(settings.mimic_target_user_id || 0),
      mimic_target_user_name: String(settings.mimic_target_user_name || ""),
      mimic_profile_text: String(settings.mimic_profile_text || ""),
      mimic_sample_count: Number(settings.mimic_sample_count || 0),
      mimic_distilled_at_count: Number(settings.mimic_distilled_at_count || 0),
    };
  }

  function normalizeDefaultPermissions(config) {
    if (!config || typeof config !== "object" || !config.base || typeof config.base !== "object") return null;
    const fields = state.permissionFields?.length ? state.permissionFields : PERMISSION_FIELD_FALLBACK;
    const base = Object.fromEntries(fields.map(({ key }) => [key, config.base[key] === true]));
    const windows = Array.isArray(config.windows) ? config.windows.map((window, index) => ({
      id: String(window?.id || `window_${index + 1}`),
      name: String(window?.name || `时段 ${index + 1}`),
      enabled: window?.enabled !== false,
      start: String(window?.start || "23:00"),
      end: String(window?.end || "07:00"),
      days: Array.isArray(window?.days)
        ? [...new Set(window.days.map(Number).filter(day => Number.isInteger(day) && day >= 0 && day <= 6))].sort()
        : [0, 1, 2, 3, 4, 5, 6],
      priority: Number.isInteger(Number(window?.priority)) ? Number(window.priority) : 0,
      overrides: Object.fromEntries(Object.entries(window?.overrides || {})
        .filter(([key, value]) => fields.some(field => field.key === key) && typeof value === "boolean")),
    })) : [];
    return {
      version: 1,
      timezone: String(config.timezone || "Asia/Shanghai"),
      schedule_enabled: config.schedule_enabled === true,
      base,
      windows,
    };
  }

  function normalizeTemplateButtons(buttons) {
    if (!Array.isArray(buttons)) return [];
    return buttons.slice(0, 12).map((button, index) => ({
      text: String(button?.text || ""),
      action: ["url", "copy", "share", "dismiss"].includes(button?.action)
        ? button.action
        : "url",
      value: String(button?.value || ""),
      row: Number.isInteger(Number(button?.row)) ? Number(button.row) : index,
    })).filter(button => button.text);
  }

  function escapeTemplateButtonPart(value) {
    return String(value ?? "")
      .replaceAll("\\", "\\\\")
      .replaceAll("|", "\\|")
      .replaceAll("\r", "\\r")
      .replaceAll("\n", "\\n");
  }

  function splitTemplateButtonLine(line) {
    const parts = [""];
    const source = String(line ?? "");
    for (let index = 0; index < source.length; index += 1) {
      const char = source[index];
      const next = source[index + 1];
      if (char === "\\" && ["\\", "|", "n", "r"].includes(next)) {
        parts[parts.length - 1] += next === "n" ? "\n" : next === "r" ? "\r" : next;
        index += 1;
      } else if (char === "|") {
        parts.push("");
      } else {
        parts[parts.length - 1] += char;
      }
    }
    return parts;
  }

  function templateButtonsToText(buttons) {
    return normalizeTemplateButtons(buttons).map(button =>
      [button.text, button.action, button.value, Number(button.row) + 1]
        .map(escapeTemplateButtonPart)
        .join(" | "),
    ).join("\n");
  }

  function parseTemplateButtonsText(raw) {
    const lines = String(raw || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (lines.length > 12) throw new Error("内联按钮最多 12 个");
    return lines.map((line, index) => {
      const parts = splitTemplateButtonLine(line);
      if (parts.length > 4) {
        throw new Error(`第 ${index + 1} 行包含未转义的“|”，请写成 \\|`);
      }
      while (parts.length < 4) parts.push("");
      const normalizedParts = parts.map(part => part.trim());
      const text = normalizedParts[0] || "";
      const action = (normalizedParts[1] || "url").toLowerCase();
      const value = normalizedParts[2] || "";
      const rowValue = normalizedParts[3] || String(index + 1);
      const row = Number(rowValue) - 1;
      if (!text) throw new Error(`第 ${index + 1} 行缺少按钮名称`);
      if (text.length > 64) throw new Error(`第 ${index + 1} 行按钮名称不能超过 64 字`);
      if (!["url", "copy", "share", "dismiss"].includes(action)) {
        throw new Error(`第 ${index + 1} 行操作无效，请使用 url/copy/share/dismiss`);
      }
      if (action === "url" && !/^(https?:\/\/|tg:\/\/)/i.test(value)) {
        throw new Error(`第 ${index + 1} 行需要有效的 http(s):// 或 tg:// 链接`);
      }
      if (["copy", "share"].includes(action) && !value) {
        throw new Error(`第 ${index + 1} 行需要填写复制/分享内容`);
      }
      if (["copy", "share"].includes(action) && value.length > 256) {
        throw new Error(`第 ${index + 1} 行复制/分享内容不能超过 256 字`);
      }
      if (action === "url" && value.length > 2048) {
        throw new Error(`第 ${index + 1} 行链接不能超过 2048 字`);
      }
      if (!Number.isInteger(row) || row < 0 || row > 7) {
        throw new Error(`第 ${index + 1} 行的行号需为 1-8`);
      }
      return { text, action, value: action === "dismiss" ? "" : value, row };
    });
  }

  function groupTemplateButtonsText(group) {
    const draft = state.groupTemplateButtonDrafts.get(String(group.id));
    return draft ? draft.raw : templateButtonsToText(group.settings.welcome_buttons);
  }

  function groupToggle(group, key, label, hint = "", disabled = false) {
    const id = `group-${group.id}-${key}`;
    return `
      <div class="toggle-field">
        <div class="toggle-copy"><strong>${escapeHtml(label)}</strong>${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</div>
        <label class="toggle" for="${attr(id)}">
          <input id="${attr(id)}" type="checkbox" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="boolean" aria-label="${attr(label)}"${group.settings[key] ? " checked" : ""}${disabled ? " disabled" : ""}>
          <span class="toggle-track"></span>
        </label>
      </div>`;
  }

  function renderCallAdminTargets(group, saving) {
    const key = String(group.id);
    const admins = state.groupTelegramAdmins.get(key);
    if (!admins) {
      return `<button class="secondary-button" type="button" data-action="load-call-admin-targets" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>${icon("users")}<span>加载管理员列表</span></button>
        <span class="field-hint">默认全部勾选；仅勾选部分时只 @ 勾选的管理员</span>`;
    }
    if (admins.loading) {
      return `<div class="loading-inline"><span class="spinner spinner-small"></span><span>正在加载</span></div>`;
    }
    if (admins.error) {
      return `<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(admins.error)}</span></div>`;
    }
    if (!admins.items.length) {
      return `<span class="field-hint">未找到可选管理员（bot 需要在群内且能读取管理员列表）</span>`;
    }
    const selected = new Set(group.settings.call_admin_targets || []);
    const selectAll = selected.size === 0;
    return `
      <div class="call-admin-target-list">
        ${admins.items.map(admin => `
        <label class="compact-check">
          <input type="checkbox" data-call-admin-target="${attr(admin.user_id)}" data-group-id="${attr(group.id)}"${selectAll || selected.has(Number(admin.user_id)) ? " checked" : ""}${saving ? " disabled" : ""}>
          <span>${escapeHtml(admin.display_name || admin.user_id)}</span>
        </label>`).join("")}
      </div>
      <span class="field-hint">全部勾选＝@所有管理员（默认，新管理员自动包含）；仅勾选部分时只 @ 勾选的人</span>`;
  }

  function renderGroupPermissions(group, saving) {
    const config = group.settings.default_permissions;
    if (!config) {
      return `
        <div class="field group-permissions-editor">
          <div class="field-label-row"><span class="field-label">群默认用户权限与定时模式</span></div>
          <p class="field-hint">尚未接管此群的默认权限。先读取 Telegram 当前值，再按群配置完整权限和夜间时段。</p>
          <button class="secondary-button" type="button" data-action="load-group-permissions" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>${icon("download")}<span>读取当前群权限</span></button>
        </div>`;
    }
    const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
    const fields = state.permissionFields?.length ? state.permissionFields : PERMISSION_FIELD_FALLBACK;
    return `
      <div class="field group-permissions-editor">
        <div class="permission-editor-head">
          <div>
            <span class="field-label">群默认用户权限与定时模式</span>
            <span class="field-hint">基础权限始终完整保存；活动时段只覆盖选中的权限，结束后自动恢复基础权限。</span>
          </div>
          <button class="secondary-button" type="button" data-action="add-permission-window" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>${icon("plus")}<span>新增时段</span></button>
        </div>
        <div class="permission-config-row">
          <label class="compact-check permission-schedule-toggle">
            <input type="checkbox" data-permission-control data-permission-config="schedule_enabled" data-group-id="${attr(group.id)}"${config.schedule_enabled ? " checked" : ""}${saving ? " disabled" : ""}>
            <span>启用定时权限</span>
          </label>
          <label class="permission-timezone">时区
            <input type="text" data-permission-control data-permission-config="timezone" data-group-id="${attr(group.id)}" value="${attr(config.timezone)}" maxlength="64" required${saving ? " disabled" : ""}>
          </label>
        </div>
        <h5>基础默认权限</h5>
        <div class="permission-field-grid">
          ${fields.map(({ key, label }) => `
            <label class="compact-check">
              <input type="checkbox" data-permission-control data-permission-base="${attr(key)}" data-group-id="${attr(group.id)}"${config.base[key] ? " checked" : ""}${saving ? " disabled" : ""}>
              <span>${escapeHtml(label)}</span>
            </label>`).join("")}
        </div>
        <div class="permission-window-list">
          ${config.windows.map((window, index) => `
            <section class="permission-window-card">
              <div class="permission-window-head">
                <strong>${escapeHtml(window.name || `时段 ${index + 1}`)}</strong>
                <button class="mini-icon-button danger" type="button" data-action="remove-permission-window" data-group-id="${attr(group.id)}" data-window-index="${index}" aria-label="删除时段" title="删除时段"${saving ? " disabled" : ""}>${icon("trash-2")}</button>
              </div>
              <div class="permission-window-fields">
                <label>名称<input type="text" data-permission-control data-permission-window-field="name" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.name)}" maxlength="80" required${saving ? " disabled" : ""}></label>
                <label>开始<input type="time" data-permission-control data-permission-window-field="start" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.start)}" required${saving ? " disabled" : ""}></label>
                <label>结束<input type="time" data-permission-control data-permission-window-field="end" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.end)}" required${saving ? " disabled" : ""}></label>
                <label>优先级<input type="number" data-permission-control data-permission-window-field="priority" data-window-kind="number" data-window-index="${index}" data-group-id="${attr(group.id)}" value="${attr(window.priority)}" min="-1000" max="1000" step="1" required${saving ? " disabled" : ""}></label>
                <label class="compact-check"><input type="checkbox" data-permission-control data-permission-window-field="enabled" data-window-kind="boolean" data-window-index="${index}" data-group-id="${attr(group.id)}"${window.enabled ? " checked" : ""}${saving ? " disabled" : ""}><span>启用时段</span></label>
              </div>
              <div class="permission-weekdays" aria-label="生效星期">
                ${weekdays.map((label, day) => `<label class="compact-check"><input type="checkbox" data-permission-control data-permission-window-day="${day}" data-window-index="${index}" data-group-id="${attr(group.id)}"${window.days.includes(day) ? " checked" : ""}${saving ? " disabled" : ""}><span>周${label}</span></label>`).join("")}
              </div>
              <div class="permission-override-grid">
                ${fields.map(({ key, label }) => {
                  const value = Object.hasOwn(window.overrides, key) ? String(window.overrides[key]) : "";
                  return `<label>${escapeHtml(label)}<select data-permission-control data-permission-window-override="${attr(key)}" data-window-index="${index}" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}><option value=""${value === "" ? " selected" : ""}>不覆盖</option><option value="true"${value === "true" ? " selected" : ""}>允许</option><option value="false"${value === "false" ? " selected" : ""}>禁止</option></select></label>`;
                }).join("")}
              </div>
            </section>`).join("") || `<p class="field-hint">暂无定时时段；可新增“23:00–07:00 禁止发送图片”等夜间模式。</p>`}
        </div>
      </div>`;
  }

  function renderGroupCard(group) {
    const dirty = groupDirty(group);
    const saving = state.groupSaving.has(String(group.id));
    const searchText = `${group.title || ""} ${group.id}`.toLowerCase();
    return `
      <article class="item-card group-card" data-group-card data-search="${attr(searchText)}">
        <div class="item-card-header">
          <div class="item-card-title">
            ${icon("users")}
            <div class="group-title-block"><strong>${escapeHtml(group.title || "未命名群组")}</strong><small>${escapeHtml(group.id)}</small></div>
          </div>
          <div class="item-actions">
            ${dirty ? `<span class="group-save-state">未保存</span>` : ""}
            <button class="secondary-button" type="button" data-action="save-group" data-group-id="${attr(group.id)}"${dirty && !saving ? "" : " disabled"}>
              ${saving ? `<span class="spinner spinner-small"></span>` : icon("save")}
              <span>${saving ? "保存中" : "保存"}</span>
            </button>
          </div>
        </div>
        <div class="item-card-body">
          <div class="group-settings-grid">
            ${groupToggle(group, "av_enabled", "允许 AV 检索", "", saving)}
            ${groupToggle(group, "mute_all_replies", "暂停全部回复", "", saving)}
            ${groupToggle(group, "at_reply_mode", "仅 @ 时回复", "", saving)}
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-join-verification-enabled">入群验证</label>
              <select id="group-${attr(group.id)}-join-verification-enabled" data-group-id="${attr(group.id)}" data-group-key="join_verification_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.join_verification_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.join_verification_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.join_verification_enabled === false ? " selected" : ""}>关闭</option>
              </select>
              <span class="field-hint">验证服务在下方单独选择</span>
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-join-verification-provider">入群验证服务</label>
              <select id="group-${attr(group.id)}-join-verification-provider" data-group-id="${attr(group.id)}" data-group-key="join_verification_provider" data-kind="nullable-string"${saving ? " disabled" : ""}>
                <option value=""${group.settings.join_verification_provider == null ? " selected" : ""}>继承全局默认</option>
                <option value="turnstile"${group.settings.join_verification_provider === "turnstile" ? " selected" : ""}>Cloudflare Turnstile</option>
                <option value="hcaptcha"${group.settings.join_verification_provider === "hcaptcha" ? " selected" : ""}>hCaptcha</option>
                <option value="turnstile_hcaptcha"${group.settings.join_verification_provider === "turnstile_hcaptcha" ? " selected" : ""}>Turnstile + hCaptcha（双重验证）</option>
              </select>
            </div>
            <div class="field welcome-message">
              <label class="field-label" for="group-${attr(group.id)}-welcome">入群欢迎语</label>
              <textarea id="group-${attr(group.id)}-welcome" data-group-id="${attr(group.id)}" data-group-key="welcome_message" data-kind="string" maxlength="4000" placeholder="留空不发送欢迎语；支持换行和 Markdown；{name} 为名称，{mention} 为可点击提及"${saving ? " disabled" : ""}>${escapeHtml(group.settings.welcome_message)}</textarea>
              <span class="field-hint">开启入群验证时在验证通过后发送；支持 **粗体**、*斜体*、[文字](链接) 等 Markdown</span>
            </div>
            <div class="field welcome-buttons">
              <label class="field-label" for="group-${attr(group.id)}-welcome-buttons">欢迎语内联按钮</label>
              <textarea id="group-${attr(group.id)}-welcome-buttons" data-template-buttons data-group-id="${attr(group.id)}" data-group-key="welcome_buttons" maxlength="30000" placeholder="每行：按钮名 | 操作 | 内容 | 行号&#10;官网 | url | https://example.com | 1&#10;复制群规 | copy | 群规文本 | 1&#10;管理员删除 | dismiss | | 2"${saving ? " disabled" : ""}>${escapeHtml(groupTemplateButtonsText(group))}</textarea>
              <span class="field-hint">操作：url 跳转、copy 复制、share 分享、dismiss 管理员删除；相同行号横向排列。内容中的 |、换行和反斜杠分别写成 \\|、\\n、\\\\</span>
            </div>
            ${renderGroupPermissions(group, saving)}
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-patrol-enabled">自动巡检</label>
              <select id="group-${attr(group.id)}-patrol-enabled" data-group-id="${attr(group.id)}" data-group-key="patrol_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.patrol_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.patrol_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.patrol_enabled === false ? " selected" : ""}>关闭</option>
              </select>
              <button class="secondary-button patrol-trigger" type="button" data-action="trigger-patrol" data-group-id="${attr(group.id)}"${saving ? " disabled" : ""}>
                ${icon("radar")}<span>立即巡检</span>
              </button>
              <span class="field-hint">巡检时间等参数在「审核验证」页配置</span>
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-raid-guard-enabled">爆破防护</label>
              <select id="group-${attr(group.id)}-raid-guard-enabled" data-group-id="${attr(group.id)}" data-group-key="raid_guard_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.raid_guard_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.raid_guard_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.raid_guard_enabled === false ? " selected" : ""}>关闭</option>
              </select>
              <span class="field-hint">锁群阈值与超时可在下方逐群覆盖</span>
            </div>
            ${RAID_GUARD_GROUP_INT_FIELDS.map(({ key, label, min, max }) => `
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-${key.replaceAll("_", "-")}">爆破防护 · ${escapeHtml(label)}</label>
              <input id="group-${attr(group.id)}-${key.replaceAll("_", "-")}" type="number" min="${min}" max="${max}" step="1" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="nullable-int" value="${attr(group.settings[key] == null ? "" : group.settings[key])}" placeholder="继承全局默认"${saving ? " disabled" : ""}>
            </div>`).join("")}
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-call-admin-enabled">呼叫管理员（@admin）</label>
              <select id="group-${attr(group.id)}-call-admin-enabled" data-group-id="${attr(group.id)}" data-group-key="call_admin_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.call_admin_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.call_admin_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.call_admin_enabled === false ? " selected" : ""}>关闭</option>
              </select>
              <span class="field-hint">群成员发送 @admin 时 @ 下方勾选的管理员；可回复消息举报</span>
            </div>
            <div class="field call-admin-targets">
              <label class="field-label">呼叫目标管理员</label>
              ${renderCallAdminTargets(group, saving)}
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-vote-ban-enabled">民主投票封禁</label>
              <select id="group-${attr(group.id)}-vote-ban-enabled" data-group-id="${attr(group.id)}" data-group-key="vote_ban_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.vote_ban_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.vote_ban_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.vote_ban_enabled === false ? " selected" : ""}>关闭</option>
              </select>
              <span class="field-hint">命令和 Bot 技能共享下方单用户额度；票数达标即封禁被回复用户</span>
            </div>
            ${VOTE_BAN_GROUP_INT_FIELDS.map(({ key, label, min, max }) => `
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-${key.replaceAll("_", "-")}">民主投票 · ${escapeHtml(label)}</label>
              <input id="group-${attr(group.id)}-${key.replaceAll("_", "-")}" type="number" min="${min}" max="${max}" step="1" data-group-id="${attr(group.id)}" data-group-key="${key}" data-kind="nullable-int" value="${attr(group.settings[key] == null ? "" : group.settings[key])}" placeholder="继承全局默认"${saving ? " disabled" : ""}>
            </div>`).join("")}
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-proactive">主动发言</label>
              <select id="group-${attr(group.id)}-proactive" data-group-id="${attr(group.id)}" data-group-key="proactive_enabled" data-kind="nullable-boolean"${saving ? " disabled" : ""}>
                <option value=""${group.settings.proactive_enabled == null ? " selected" : ""}>继承全局默认</option>
                <option value="true"${group.settings.proactive_enabled === true ? " selected" : ""}>开启</option>
                <option value="false"${group.settings.proactive_enabled === false ? " selected" : ""}>关闭</option>
              </select>
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-tts">语音模式</label>
              <select id="group-${attr(group.id)}-tts" data-group-id="${attr(group.id)}" data-group-key="tts_mode" data-kind="string"${saving ? " disabled" : ""}>
                <option value="off"${group.settings.tts_mode === "off" ? " selected" : ""}>关闭</option>
                <option value="on"${group.settings.tts_mode === "on" ? " selected" : ""}>允许按需语音</option>
                <option value="always"${group.settings.tts_mode === "always" ? " selected" : ""}>始终发送语音</option>
              </select>
            </div>
            <div class="field proactive-brief">
              <label class="field-label" for="group-${attr(group.id)}-brief">主动任务简述</label>
              <textarea id="group-${attr(group.id)}-brief" data-group-id="${attr(group.id)}" data-group-key="proactive_task_brief" data-kind="string" maxlength="240" placeholder="留空使用默认主动话题策略"${saving ? " disabled" : ""}>${escapeHtml(group.settings.proactive_task_brief)}</textarea>
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-mimic-id">风格目标用户 ID</label>
              <input id="group-${attr(group.id)}-mimic-id" type="number" min="0" step="1" data-group-id="${attr(group.id)}" data-group-key="mimic_target_user_id" data-kind="nonnegative-int" value="${attr(group.settings.mimic_target_user_id)}"${saving ? " disabled" : ""}>
              <span class="field-hint">更换或清除 ID 会重置已有名称、画像、计数和样本</span>
            </div>
            <div class="field">
              <label class="field-label" for="group-${attr(group.id)}-mimic-name">风格目标名称</label>
              <input id="group-${attr(group.id)}-mimic-name" type="text" maxlength="80" data-group-id="${attr(group.id)}" data-group-key="mimic_target_user_name" data-kind="string" value="${attr(group.settings.mimic_target_user_name)}"${saving ? " disabled" : ""}>
            </div>
            <div class="field mimic-profile">
              <label class="field-label" for="group-${attr(group.id)}-mimic-profile">说话风格画像</label>
              <textarea id="group-${attr(group.id)}-mimic-profile" data-group-id="${attr(group.id)}" data-group-key="mimic_profile_text" data-kind="string" maxlength="1200" placeholder="可手动编辑，也会由采样自动更新"${saving ? " disabled" : ""}>${escapeHtml(group.settings.mimic_profile_text)}</textarea>
              <span class="field-hint">已采样 ${escapeHtml(group.settings.mimic_sample_count)} 条，最近蒸馏点 ${escapeHtml(group.settings.mimic_distilled_at_count)} 条</span>
            </div>
          </div>
          <div class="subsection group-resource-panel" data-group-resource-panel="${attr(group.id)}">
            <div class="subsection-head">
              <strong>群规、永久记忆与成员策略</strong>
              <button class="secondary-button" type="button" data-action="load-group-resources" data-group-id="${attr(group.id)}">${icon("list-tree")}管理</button>
            </div>
            ${renderGroupResources(group)}
          </div>
        </div>
      </article>`;
  }

  function renderKeywordReplyRow(group, item) {
    return `
      <form class="rule-resource-form entry-edit-form" data-entry-edit-form="keyword-replies" data-group-id="${attr(group.id)}" data-entry-id="${attr(item.id)}">
        <select name="match_type" aria-label="匹配方式">
          <option value="contains"${item.match_type === "contains" ? " selected" : ""}>包含</option>
          <option value="exact"${item.match_type === "exact" ? " selected" : ""}>完全匹配</option>
          <option value="regex"${item.match_type === "regex" ? " selected" : ""}>正则</option>
        </select>
        <input name="keyword" maxlength="255" value="${attr(item.keyword)}" aria-label="关键词" required>
        <textarea name="reply_text" maxlength="4000" rows="3" aria-label="回复内容" required>${escapeHtml(item.reply_text)}</textarea>
        <textarea name="buttons_text" maxlength="30000" rows="2" aria-label="内联按钮" placeholder="按钮名 | url/copy/share/dismiss | 内容 | 行号；内容中 | 写成 \\|">${escapeHtml(templateButtonsToText(item.buttons))}</textarea>
        <label class="compact-check"><input name="pin_message" type="checkbox"${item.pin_message ? " checked" : ""}><span>置顶</span></label>
        <label class="compact-check"><input name="auto_delete" type="checkbox"${item.auto_delete ? " checked" : ""}><span>自动删除</span></label>
        <label class="compact-check"><input name="enabled" type="checkbox"${item.enabled ? " checked" : ""}><span>启用</span></label>
        <button class="mini-icon-button" type="submit" aria-label="保存" title="保存">${icon("save")}</button>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="keyword-replies" data-resource-id="${attr(item.id)}" aria-label="删除" title="删除">${icon("trash-2")}</button>
      </form>`;
  }

  function renderScheduledMessageRow(group, item) {
    return `
      <form class="rule-resource-form entry-edit-form" data-entry-edit-form="scheduled-messages" data-group-id="${attr(group.id)}" data-entry-id="${attr(item.id)}">
        <textarea name="text" maxlength="4000" rows="3" aria-label="消息内容" required>${escapeHtml(item.text)}</textarea>
        <textarea name="buttons_text" maxlength="30000" rows="2" aria-label="内联按钮" placeholder="按钮名 | url/copy/share/dismiss | 内容 | 行号；内容中 | 写成 \\|">${escapeHtml(templateButtonsToText(item.buttons))}</textarea>
        <select name="schedule_type" aria-label="定时方式">
          <option value="daily"${item.schedule_type === "daily" ? " selected" : ""}>每天定时</option>
          <option value="interval"${item.schedule_type === "interval" ? " selected" : ""}>固定间隔</option>
        </select>
        <input name="schedule_time" type="time" value="${attr(item.schedule_time)}" aria-label="发送时间" title="每天定时的发送时间">
        <input name="interval_minutes" type="number" min="5" max="10080" step="1" value="${attr(item.interval_minutes)}" aria-label="间隔分钟" title="固定间隔的分钟数" required>
        <label class="compact-check"><input name="pin_message" type="checkbox"${item.pin_message ? " checked" : ""}><span>置顶</span></label>
        <label class="compact-check"><input name="unpin_previous" type="checkbox"${item.unpin_previous ? " checked" : ""}><span>取消上次置顶</span></label>
        <label class="compact-check"><input name="auto_delete" type="checkbox"${item.auto_delete ? " checked" : ""}><span>自动删除</span></label>
        <label class="compact-check"><input name="enabled" type="checkbox"${item.enabled ? " checked" : ""}><span>启用</span></label>
        <button class="mini-icon-button" type="submit" aria-label="保存" title="保存">${icon("save")}</button>
        <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="scheduled-messages" data-resource-id="${attr(item.id)}" aria-label="删除" title="删除">${icon("trash-2")}</button>
      </form>`;
  }

  function renderGroupResources(group) {
    const resource = state.groupResources.get(String(group.id));
    if (!resource) return `<p class="field-hint">打开后可直接管理关键词回复、定时消息、群规、永久记忆、警告记录、群内封禁、审核豁免和回复静默名单。</p>`;
    if (resource.loading) return `<div class="loading-inline"><span class="spinner spinner-small"></span><span>正在加载</span></div>`;
    if (resource.error) return `<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(resource.error)}</span></div>`;
    const listKey = type => `group:${group.id}:${type}`;
    const userLabel = item => {
      const name = String(item.display_name || item.username || `用户 ${item.user_id}`);
      const username = String(item.username || "").replace(/^@/, "");
      const usernamePart = username && name !== `@${username}` ? ` · @${username}` : "";
      return `${name}${usernamePart} · ${item.user_id}`;
    };
    const itemRows = (items, type, text) => paginatedRows(items, listKey(type), item => `
      <div class="resource-row">
        <span>${escapeHtml(text(item))}</span>
        <span class="resource-row-actions">
          ${type === "memories" ? `<button class="mini-icon-button" type="button" data-action="edit-group-resource" data-group-id="${attr(group.id)}" data-resource-type="${type}" data-resource-id="${attr(item.id)}" aria-label="编辑" title="编辑">${icon("pencil")}</button>` : ""}
          ${(type !== "warnings" || !item.is_banned) ? `<button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="${type}" data-resource-id="${attr(item.id ?? item.user_id)}" aria-label="${type === "warnings" ? "清零警告" : "删除"}" title="${type === "warnings" ? "清零警告" : "删除"}">${icon("trash-2")}</button>` : ""}
        </span>
      </div>`);
    return `
      <div class="resource-grid">
        <section class="resource-span">
          <h4>关键词回复</h4>
          <form class="inline-resource-form template-resource-form" data-resource-form="keyword-replies" data-group-id="${attr(group.id)}">
            <select name="match_type" aria-label="匹配方式"><option value="contains">包含</option><option value="exact">完全匹配</option><option value="regex">正则</option></select>
            <input name="keyword" maxlength="255" placeholder="关键词" required>
            <textarea name="reply_text" maxlength="4000" rows="3" placeholder="回复内容（支持换行和 Markdown）" required></textarea>
            <textarea name="buttons_text" maxlength="30000" rows="2" placeholder="可选内联按钮：按钮名 | url/copy/share/dismiss | 内容 | 行号；| 写成 \\|"></textarea>
            <label class="compact-check"><input name="pin_message" type="checkbox"><span>置顶</span></label>
            <label class="compact-check"><input name="auto_delete" type="checkbox" checked><span>自动删除</span></label>
            <button class="icon-button" type="submit" aria-label="新增关键词回复" title="新增关键词回复">${icon("plus")}</button>
          </form>
          <p class="field-hint">命中后直接发送固定回复并跳过 AI；内容支持换行和 Markdown。按钮同一行号会横向排列；「自动删除」按全局关键词类别执行。</p>
          ${paginatedRows(resource.keyword_replies, listKey("keyword-replies"), item => renderKeywordReplyRow(group, item))}
        </section>
        <section class="resource-span">
          <h4>定时消息</h4>
          <form class="inline-resource-form template-resource-form" data-resource-form="scheduled-messages" data-group-id="${attr(group.id)}">
            <textarea name="text" maxlength="4000" rows="3" placeholder="消息内容（支持换行和 Markdown）" required></textarea>
            <textarea name="buttons_text" maxlength="30000" rows="2" placeholder="可选内联按钮：按钮名 | url/copy/share/dismiss | 内容 | 行号；| 写成 \\|"></textarea>
            <select name="schedule_type" aria-label="定时方式"><option value="daily">每天定时</option><option value="interval">固定间隔</option></select>
            <input name="schedule_time" type="time" value="09:00" aria-label="发送时间" title="每天定时的发送时间">
            <input name="interval_minutes" type="number" min="5" max="10080" step="1" value="60" aria-label="间隔分钟" title="固定间隔的分钟数" required>
            <label class="compact-check"><input name="pin_message" type="checkbox"><span>置顶</span></label>
            <label class="compact-check"><input name="auto_delete" type="checkbox"><span>自动删除</span></label>
            <button class="icon-button" type="submit" aria-label="新增定时消息" title="新增定时消息">${icon("plus")}</button>
          </form>
          <p class="field-hint">内容支持换行和 Markdown；「每天定时」按发送时间触发，「固定间隔」按分钟循环。按钮同一行号会横向排列。</p>
          ${paginatedRows(resource.scheduled_messages, listKey("scheduled-messages"), item => renderScheduledMessageRow(group, item))}
        </section>
        <section>
          <h4>群规</h4>
          <form class="inline-resource-form multiline-resource-form" data-resource-form="rules" data-group-id="${attr(group.id)}">
            <select name="rule_type"><option value="keyword">关键词</option><option value="regex">正则</option><option value="llm">语义</option></select>
            <textarea name="pattern" maxlength="1000" rows="2" placeholder="规则内容（支持换行，可拉伸）" required></textarea>
            <select name="action"><option value="warn">警告</option><option value="delete">删消息</option><option value="ban">封禁</option></select>
            <button class="icon-button" type="submit" aria-label="新增群规" title="新增群规">${icon("plus")}</button>
          </form>
          ${paginatedRows(resource.rules, listKey("rules"), rule => `
            <form class="rule-resource-form" data-rule-edit-form data-group-id="${attr(group.id)}" data-rule-id="${attr(rule.id)}">
              <select name="rule_type" aria-label="群规类型">
                <option value="keyword"${rule.rule_type === "keyword" ? " selected" : ""}>关键词</option>
                <option value="regex"${rule.rule_type === "regex" ? " selected" : ""}>正则</option>
                <option value="llm"${rule.rule_type === "llm" ? " selected" : ""}>语义</option>
              </select>
              <textarea name="pattern" maxlength="1000" rows="2" aria-label="群规内容" required>${escapeHtml(rule.pattern)}</textarea>
              <select name="action" aria-label="命中动作">
                <option value="warn"${rule.action === "warn" ? " selected" : ""}>警告</option>
                <option value="delete"${rule.action === "delete" ? " selected" : ""}>删消息</option>
                <option value="ban"${rule.action === "ban" ? " selected" : ""}>封禁</option>
              </select>
              <label class="compact-check"><input name="enabled" type="checkbox"${rule.enabled ? " checked" : ""}><span>启用</span></label>
              <button class="mini-icon-button" type="submit" aria-label="保存群规" title="保存群规">${icon("save")}</button>
              <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="rules" data-resource-id="${attr(rule.id)}" aria-label="删除" title="删除">${icon("trash-2")}</button>
            </form>`)}
        </section>
        <section>
          <h4>永久记忆</h4>
          <form class="inline-resource-form multiline-resource-form" data-resource-form="memories" data-group-id="${attr(group.id)}">
            <textarea name="content" maxlength="4000" rows="2" placeholder="新增永久记忆（支持换行，可拉伸）" required></textarea>
            <button class="icon-button" type="submit" aria-label="新增记忆" title="新增记忆">${icon("plus")}</button>
          </form>
          ${paginatedRows(resource.memories, listKey("memories"), item => `
            <form class="rule-resource-form memory-edit-form" data-memory-edit-form data-group-id="${attr(group.id)}" data-memory-id="${attr(item.id)}">
              <textarea name="content" maxlength="4000" rows="3" aria-label="永久记忆内容" required>${escapeHtml(item.content)}</textarea>
              <button class="mini-icon-button" type="submit" aria-label="保存记忆" title="保存记忆">${icon("save")}</button>
              <button class="mini-icon-button danger" type="button" data-action="delete-group-resource" data-group-id="${attr(group.id)}" data-resource-type="memories" data-resource-id="${attr(item.id)}" aria-label="删除" title="删除">${icon("trash-2")}</button>
            </form>`)}
        </section>
        <section>
          <h4>警告与封禁记录</h4>
          ${itemRows(resource.warnings, "warnings", item => `${userLabel(item)} · ${item.count} 次${item.is_banned ? " · 已封禁（请在下方解封）" : ""}`)}
        </section>
        <section>
          <h4>群内封禁</h4>
          ${userIdForm(group.id, "bans", "输入用户 ID 后封禁")}
          ${itemRows(resource.bans, "bans", item => `${userLabel(item)} · 已封禁`)}
        </section>
        <section>
          <h4>AI 审核豁免</h4>
          ${userIdForm(group.id, "moderation-exemptions", "新增豁免用户 ID")}
          ${itemRows(resource.exemptions, "moderation-exemptions", item => userLabel(item))}
        </section>
        <section>
          <h4>回复静默名单</h4>
          ${userIdForm(group.id, "reply-mutes", "新增静默用户 ID")}
          ${itemRows(resource.reply_mutes, "reply-mutes", item => userLabel(item))}
        </section>
      </div>`;
  }

  function userIdForm(groupId, type, placeholder) {
    return `<form class="inline-resource-form" data-resource-form="${type}" data-group-id="${attr(groupId)}">
      <input name="user_id" type="number" step="1" placeholder="${attr(placeholder)}" required>
      <button class="icon-button" type="submit" aria-label="新增" title="新增">${icon("plus")}</button>
    </form>`;
  }

  function renderGroups() {
    if (state.groupsError) {
      return `
        ${pageHead("群组设置", "逐群覆盖全局行为。", `<button class="secondary-button" type="button" data-action="reload-groups">${icon("refresh-cw")}重试</button>`)}
        <div class="error-state">${icon("circle-x")}<p>${escapeHtml(state.groupsError)}</p></div>`;
    }
    return `
      ${pageHead("群组设置", "逐群配置入群验证、回复、语音、主动发言与内容检索。", `<button class="secondary-button" type="button" data-action="reload-groups"${state.groupSaving.size ? " disabled" : ""}>${icon("refresh-cw")}刷新群组</button>`)}
      <div class="group-toolbar">
        <div class="search-wrap">${icon("search")}<input id="group-search" class="search-input" type="search" placeholder="搜索群名或群 ID" autocomplete="off" value="${attr(state.groupSearch)}"></div>
        <span class="badge info">${state.groups.length} 个群组</span>
      </div>
      <div class="group-list">
        ${state.groups.map(renderGroupCard).join("") || `<div class="empty-state">${icon("users")}<p>暂无可管理群组</p></div>`}
      </div>`;
  }

  function renderAccess() {
    const access = state.access;
    if (!access) return `
      ${pageHead("权限与封禁", "管理授权群、群管理员和全局封禁名单。")}
      <button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}加载数据</button>`;
    if (access.loading) return `<div class="loading-state"><span class="spinner"></span><p>正在加载权限数据</p></div>`;
    if (access.error) return `${pageHead("权限与封禁", "管理授权群、群管理员和全局封禁名单。", `<button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}重试</button>`)}<div class="notice danger">${icon("circle-x")}<span>${escapeHtml(access.error)}</span></div>`;
    const rows = (items, type, label, idKey = "user_id", emptyText) => paginatedRows(items, `access:${type}`, item => {
      const resourceId = type === "admins"
        ? `${item.group_id}:${item.user_id}`
        : item[idKey];
      return `
      <div class="resource-row"><span>${escapeHtml(label(item))}</span>
      <button class="mini-icon-button danger" type="button" data-action="delete-access" data-access-type="${type}" data-access-id="${attr(resourceId)}" aria-label="删除" title="删除">${icon("trash-2")}</button></div>`;
    }, emptyText);
    const authGroups = access.authorized_groups || [];
    const adminGroupId = authGroups.some(group => String(group.group_id) === String(state.accessAdminGroup))
      ? String(state.accessAdminGroup)
      : String(authGroups[0]?.group_id ?? "");
    const groupAdmins = access.admins.filter(admin => String(admin.group_id) === adminGroupId);
    return `
      ${pageHead("权限与封禁", "管理授权群、群管理员和全局封禁名单。", `<button class="secondary-button" type="button" data-action="load-access">${icon("refresh-cw")}刷新</button>`)}
      <div class="resource-grid access-grid">
        <section class="settings-section">
          ${sectionHead("授权群组")}
          <form class="inline-resource-form" data-access-form="authorized-groups">
            <input name="group_id" type="number" step="1" placeholder="群 ID" required>
            <input name="title" maxlength="255" placeholder="群名称（可选）">
            <button class="icon-button" type="submit" aria-label="授权群组" title="授权群组">${icon("plus")}</button>
          </form>
          ${rows(access.authorized_groups, "authorized-groups", item => `${item.title || "未命名群组"} · ${item.group_id}`, "group_id")}
        </section>
        <section class="settings-section">
          ${sectionHead("群管理员", "先选择群组，下方仅显示并管理该群的管理员。")}
          <form class="inline-resource-form" data-access-form="admins">
            <select name="group_id" data-access-admin-filter required>${authGroups.map(item => `<option value="${attr(item.group_id)}"${String(item.group_id) === adminGroupId ? " selected" : ""}>${escapeHtml(item.title || item.group_id)}</option>`).join("")}</select>
            <input name="user_id" type="number" step="1" placeholder="用户 ID" required>
            <button class="icon-button" type="submit" aria-label="授权管理员" title="授权管理员">${icon("plus")}</button>
          </form>
          ${rows(groupAdmins, "admins", item => item.display_name ? `${item.display_name} · ${item.user_id}` : `${item.user_id}`, "user_id", "该群暂无管理员")}
        </section>
        <section class="settings-section">
          ${sectionHead("全局封禁")}
          <form class="inline-resource-form" data-access-form="global-bans">
            <input name="user_id" type="number" step="1" placeholder="用户 ID" required>
            <input name="reason" maxlength="1000" placeholder="封禁原因">
            <button class="icon-button" type="submit" aria-label="封禁用户" title="封禁用户">${icon("plus")}</button>
          </form>
          ${rows(access.global_bans, "global-bans", item => `${item.user_id} · ${item.reason || "手动封禁"}`)}
        </section>
      </div>`;
  }

  function resourceDraftFormKey(form) {
    const groupId = String(form.dataset.groupId || "");
    if (!groupId) return "";
    if (form.dataset.entryEditForm) {
      return `entry:${groupId}:${form.dataset.entryEditForm}:${form.dataset.entryId}`;
    }
    if (form.matches("[data-rule-edit-form]")) {
      return `rule:${groupId}:${form.dataset.ruleId}`;
    }
    if (form.matches("[data-memory-edit-form]")) {
      return `memory:${groupId}:${form.dataset.memoryId}`;
    }
    if (form.dataset.resourceForm) {
      return `create:${groupId}:${form.dataset.resourceForm}`;
    }
    return "";
  }

  function resourceFormSnapshot(form) {
    const snapshot = {};
    for (const control of form.elements) {
      if (!control.name) continue;
      snapshot[control.name] = control.type === "checkbox"
        ? { type: "checkbox", value: control.checked }
        : { type: control.type || control.tagName.toLowerCase(), value: control.value };
    }
    return snapshot;
  }

  function applyResourceFormSnapshot(form, snapshot) {
    for (const control of form.elements) {
      if (!control.name || !Object.hasOwn(snapshot || {}, control.name)) continue;
      const stored = snapshot[control.name];
      if (control.type === "checkbox") control.checked = Boolean(stored.value);
      else control.value = String(stored.value ?? "");
    }
  }

  function syncScheduledFormFields(form) {
    const type = form.dataset.entryEditForm || form.dataset.resourceForm;
    if (type !== "scheduled-messages") return;
    const scheduleType = form.elements.schedule_type;
    const scheduleTime = form.elements.schedule_time;
    const intervalMinutes = form.elements.interval_minutes;
    if (!scheduleType || !scheduleTime || !intervalMinutes) return;
    const daily = scheduleType.value === "daily";
    scheduleTime.disabled = !daily;
    scheduleTime.required = daily;
    intervalMinutes.disabled = daily;
    intervalMinutes.required = !daily;
    scheduleTime.title = daily ? "每天定时必须填写发送时间" : "固定间隔模式不使用发送时间";
    intervalMinutes.title = daily ? "每天定时模式不使用间隔分钟" : "固定间隔必须填写分钟数";
  }

  function captureResourceFormDraft(form) {
    const key = resourceDraftFormKey(form);
    if (!key) return;
    const current = resourceFormSnapshot(form);
    const baseline = state.resourceFormBaselines.get(key);
    if (baseline && sameValue(current, baseline)) state.resourceFormDrafts.delete(key);
    else if (baseline) state.resourceFormDrafts.set(key, current);
    else state.resourceFormBaselines.set(key, current);
  }

  function captureResourceFormDrafts() {
    content.querySelectorAll(RESOURCE_DRAFT_FORM_SELECTOR).forEach(captureResourceFormDraft);
  }

  function restoreResourceFormDrafts() {
    content.querySelectorAll(RESOURCE_DRAFT_FORM_SELECTOR).forEach(form => {
      const key = resourceDraftFormKey(form);
      if (!key) return;
      state.resourceFormBaselines.set(key, resourceFormSnapshot(form));
      const draft = state.resourceFormDrafts.get(key);
      if (draft) applyResourceFormSnapshot(form, draft);
      syncScheduledFormFields(form);
    });
  }

  function markResourceFormClean(form) {
    const key = resourceDraftFormKey(form);
    if (!key) return;
    state.resourceFormBaselines.set(key, resourceFormSnapshot(form));
    state.resourceFormDrafts.delete(key);
  }

  function clearResourceFormDrafts() {
    state.resourceFormDrafts.clear();
    state.resourceFormBaselines.clear();
  }

  function restoreGroupTemplateButtonValidity() {
    content.querySelectorAll("[data-template-buttons]").forEach(control => {
      const draft = state.groupTemplateButtonDrafts.get(String(control.dataset.groupId));
      control.setCustomValidity(draft?.error || "");
    });
  }

  function applyGroupSearchFilter() {
    const query = state.groupSearch.trim().toLowerCase();
    content.querySelectorAll("[data-group-card]").forEach(card => {
      card.hidden = Boolean(query) && !card.dataset.search.includes(query);
    });
  }

  function renderContent({ resetScroll = false } = {}) {
    if (!state.config) return;
    captureResourceFormDrafts();
    const renderers = {
      overview: renderOverview,
      models: renderModels,
      bot: renderBot,
      safety: renderSafety,
      media: renderMedia,
      integrations: renderIntegrations,
      logging: renderLogging,
      prompts: renderPrompts,
      groups: renderGroups,
      access: renderAccess,
    };
    const renderer = renderers[state.activeTab] || renderGroups;
    // Replacing innerHTML collapses the document height and lets the browser
    // clamp the scroll position; restore it so partial refreshes (saving a
    // group, loading resources) do not jump the page back to the top.
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    content.innerHTML = renderer();
    restoreResourceFormDrafts();
    restoreGroupTemplateButtonValidity();
    applyGroupSearchFilter();
    refreshIcons();
    if (resetScroll) {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    } else {
      window.scrollTo(scrollX, scrollY);
    }
  }

  function render() {
    updateChrome();
    renderContent();
    app.setAttribute("aria-busy", "false");
  }

  function renderFatal(title, message) {
    state.loading = false;
    app.setAttribute("aria-busy", "false");
    content.innerHTML = `
      <div class="error-state">
        ${icon("shield-x")}
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(message)}</p>
        <button class="secondary-button retry-button" type="button" data-action="retry-load">${icon("refresh-cw")}重试</button>
      </div>`;
    saveButton.disabled = true;
    reloadButton.disabled = false;
    refreshIcons();
  }

  function applySettingsDocument(document) {
    state.document = document;
    state.config = clone(document.config);
    if (state.config?.bot?.auto_delete_seconds == null && state.config?.bot?.auto_delete_minutes != null) {
      state.config.bot.auto_delete_seconds = Number(state.config.bot.auto_delete_minutes || 0) * 60;
    }
    if (state.config?.bot) delete state.config.bot.auto_delete_minutes;
    if (state.config?.bot && (typeof state.config.bot.auto_delete_category_seconds !== "object" || state.config.bot.auto_delete_category_seconds == null)) {
      state.config.bot.auto_delete_category_seconds = {};
    }
    if (state.config?.verification) {
      state.config.verification.provider ||= "turnstile";
      state.config.verification.hcaptcha_site_key ||= "";
      state.config.verification.hcaptcha_secret_key ||= "";
    }
    state.baseline = clone(document.config);
    state.baseline = clone(state.config);
    state.configuredSecrets = new Set(document.configured_secrets || []);
    state.secretChanges = {};
  }

  function applyGroupsDocument(document) {
    const groups = Array.isArray(document?.groups) ? document.groups : [];
    state.permissionFields = Array.isArray(document?.permission_fields) && document.permission_fields.length
      ? document.permission_fields.map(item => ({ key: String(item.key), label: String(item.label || item.key) }))
      : PERMISSION_FIELD_FALLBACK;
    state.groups = groups.map(group => ({
      id: group.id,
      title: String(group.title || ""),
      revision: String(group.revision || ""),
      settings: normalizeGroupSettings(group.settings),
    }));
    state.groupBaselines = new Map(state.groups.map(group => [String(group.id), clone(group.settings)]));
    for (const group of state.groups) {
      const draft = state.groupTemplateButtonDrafts.get(String(group.id));
      if (Array.isArray(draft?.buttons)) {
        group.settings.welcome_buttons = clone(draft.buttons);
      }
    }
    state.groupsError = "";
  }

  async function loadAll({ keepTab = true } = {}) {
    captureResourceFormDrafts();
    state.loading = true;
    updateChrome();
    if (!tg?.initData) {
      renderFatal("无法验证管理员身份", "请从 Telegram 中的管理员 Mini App 入口打开此页面。");
      return;
    }
    if (!keepTab) state.activeTab = "overview";
    content.innerHTML = `<div class="loading-state"><span class="spinner"></span><p>正在加载设置</p></div>`;
    try {
      const sessionResult = await apiFetch("/api/v1/session");
      state.session = sessionResult.session;
      if (!state.session?.can_manage_global) state.activeTab = "groups";
      const requests = [apiFetch("/api/v1/groups")];
      if (state.session?.can_manage_global) requests.unshift(apiFetch("/api/v1/settings"));
      const results = await Promise.allSettled(requests);
      const settingsResult = state.session?.can_manage_global ? results[0] : null;
      const groupsResult = state.session?.can_manage_global ? results[1] : results[0];
      if (settingsResult?.status === "rejected") throw settingsResult.reason;
      if (settingsResult?.status === "fulfilled") applySettingsDocument(settingsResult.value);
      else {
        state.document = { revision: 0, bootstrap: {}, restart_required_paths: [] };
        state.config = {};
        state.baseline = {};
      }
      if (groupsResult.status === "fulfilled") {
        applyGroupsDocument(groupsResult.value);
      } else {
        state.groups = [];
        state.groupBaselines = new Map();
        state.groupsError = groupsResult.reason?.message || "群组列表加载失败";
      }
      state.loading = false;
      render();
    } catch (error) {
      const auth = error instanceof ApiError && (error.status === 401 || error.status === 403);
      renderFatal(auth ? "没有管理权限" : "设置加载失败", error.message || "无法连接到 Mini App 服务");
    }
  }

  async function reloadGroups() {
    try {
      const document = await apiFetch("/api/v1/groups");
      applyGroupsDocument(document);
      if (state.activeTab === "groups") renderContent();
      updateChrome();
    } catch (error) {
      state.groupsError = error.message || "群组列表加载失败";
      if (state.activeTab === "groups") renderContent();
      showToast(state.groupsError, "error");
    }
  }

  async function loadCallAdminTargets(groupId) {
    const key = String(groupId);
    state.groupTelegramAdmins.set(key, { loading: true });
    renderContent();
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/telegram-admins`);
      state.groupTelegramAdmins.set(key, { items: result.admins || [] });
    } catch (error) {
      state.groupTelegramAdmins.set(key, { error: error.message || "管理员列表加载失败" });
    }
    renderContent();
  }

  async function loadGroupPermissions(groupId) {
    const group = state.groups.find(item => String(item.id) === String(groupId));
    if (!group || state.groupSaving.has(String(group.id))) return;
    try {
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/default-permissions`);
      if (Array.isArray(result.permission_fields) && result.permission_fields.length) {
        state.permissionFields = result.permission_fields;
      }
      group.settings.default_permissions = normalizeDefaultPermissions(result.default_permissions);
      renderContent();
      updateChrome();
      showToast(
        result.repaired
          ? "旧权限配置已兼容修复，请检查后保存"
          : result.configured
            ? "已加载保存的群权限"
            : "已读取 Telegram 当前群权限，请检查后保存",
        result.repaired ? "warning" : "success",
        result.repaired ? 6000 : 3600,
      );
    } catch (error) {
      showToast(error.message || "读取群默认权限失败", "error", 6000);
    }
  }

  async function loadGroupResources(groupId) {
    const key = String(groupId);
    state.groupResources.set(key, { loading: true });
    renderContent();
    try {
      const base = `/api/v1/groups/${encodeURIComponent(groupId)}`;
      const [rules, memories, warnings, bans, exemptions, replyMutes, keywordReplies, scheduledMessages] = await Promise.all([
        apiFetch(`${base}/rules`),
        apiFetch(`${base}/memories`),
        apiFetch(`${base}/warnings`),
        apiFetch(`${base}/bans`),
        apiFetch(`${base}/moderation-exemptions`),
        apiFetch(`${base}/reply-mutes`),
        apiFetch(`${base}/keyword-replies`),
        apiFetch(`${base}/scheduled-messages`),
      ]);
      state.groupResources.set(key, {
        rules: rules.rules || [],
        memories: memories.memories || [],
        warnings: warnings.warnings || [],
        bans: bans.bans || [],
        exemptions: exemptions.exemptions || [],
        reply_mutes: replyMutes.reply_mutes || [],
        keyword_replies: keywordReplies.keyword_replies || [],
        scheduled_messages: scheduledMessages.scheduled_messages || [],
      });
    } catch (error) {
      state.groupResources.set(key, { error: error.message || "群管理数据加载失败" });
    }
    renderContent();
  }

  async function loadAccess() {
    state.access = { loading: true };
    renderContent();
    try {
      const groupsResult = await apiFetch("/api/v1/authorized-groups");
      const authorizedGroups = groupsResult.authorized_groups || [];
      const loadGlobalBans = async () => {
        const all = [];
        let offset = 0;
        for (let page = 0; page < 100; page += 1) {
          const result = await apiFetch(`/api/v1/global-bans?limit=500&offset=${offset}`);
          all.push(...(result.global_bans || []));
          if (result.next_offset == null || Number(result.next_offset) <= offset) break;
          offset = Number(result.next_offset);
        }
        return all;
      };
      const [adminResults, bansResult] = await Promise.all([
        Promise.all(authorizedGroups.map(async group => {
          const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.group_id)}/admins`);
          return (result.admins || []).map(admin => ({ ...admin, group_id: group.group_id }));
        })),
        loadGlobalBans(),
      ]);
      state.access = {
        authorized_groups: authorizedGroups,
        admins: adminResults.flat(),
        global_bans: bansResult,
      };
    } catch (error) {
      state.access = { error: error.message || "权限数据加载失败", authorized_groups: [], admins: [], global_bans: [] };
    }
    renderContent();
  }

  function stripSecrets(config) {
    const payload = clone(config);
    payload.verification.turnstile_secret_key = "";
    payload.verification.hcaptcha_secret_key = "";
    payload.tts.app_key = "";
    payload.tts.access_key = "";
    payload.sub2api.api_key = "";
    for (const provider of payload.models.providers) provider.api_key = "";
    return payload;
  }

  function validateConfig() {
    const providers = state.config.models.providers;
    if (!providers.length) return "至少需要一个模型供应商";
    const names = providers.map(provider => String(provider.name || "").trim().toLowerCase());
    if (names.some(name => !/^[a-z0-9_-]{1,64}$/.test(name))) return "供应商配置名称格式不正确";
    if (new Set(names).size !== names.length) return "供应商配置名称不能重复";
    providers.forEach((provider, index) => { provider.name = names[index]; });
    const known = new Set(names);
    if (!known.has(state.config.models.main.provider)) return "主模型必须选择有效供应商";
    if (!String(state.config.models.main.model || "").trim()) return "主模型名称不能为空";
    for (const roleName of Object.keys(ROLE_META)) {
      const role = state.config.models[roleName];
      if (role.provider && !known.has(role.provider)) return `${ROLE_META[roleName].label}引用了不存在的供应商`;
      for (const fallback of role.fallbacks || []) {
        if (!known.has(fallback.provider)) return `${ROLE_META[roleName].label}的回退供应商不存在`;
      }
    }
    const pendingTurnstileSecret = state.secretChanges["verification.turnstile_secret_key"];
    if (
      pendingTurnstileSecret?.action === "replace"
      && String(pendingTurnstileSecret.value || "").trim()
        === String(state.config.verification.turnstile_site_key || "").trim()
    ) {
      return "Turnstile Secret Key 不能与 Site Key 相同";
    }
    const pendingHcaptchaSecret = state.secretChanges["verification.hcaptcha_secret_key"];
    if (
      pendingHcaptchaSecret?.action === "replace"
      && String(pendingHcaptchaSecret.value || "").trim()
        === String(state.config.verification.hcaptcha_site_key || "").trim()
    ) {
      return "hCaptcha Secret Key 不能与 Site Key 相同";
    }
    const nullPath = findNullNumber(state.config);
    if (nullPath) return `${nullPath} 需要填写有效数字`;
    const invalid = content.querySelector("input:invalid, select:invalid, textarea:invalid");
    if (invalid) {
      invalid.reportValidity();
      return "请修正标记的字段";
    }
    const replacingSecret = Object.values(state.secretChanges).some(change => change.action === "replace");
    if (replacingSecret && !state.document.bootstrap?.master_key_configured) return "需要先在部署环境配置 CONFIG_MASTER_KEY，才能保存密钥";
    return "";
  }

  function findNullNumber(value, path = "") {
    if (value === null) return path;
    if (!value || typeof value !== "object") return "";
    for (const [key, child] of Object.entries(value)) {
      const found = findNullNumber(child, path ? `${path}.${key}` : key);
      if (found) return found;
    }
    return "";
  }

  function applySecretResults() {
    const next = new Set(state.configuredSecrets);
    for (const [path, change] of Object.entries(state.secretChanges)) {
      if (change.action === "clear") next.delete(path);
      if (change.action === "replace") next.add(path);
    }
    const allowedProviderSecrets = new Set(state.config.models.providers.map(provider => `providers.${provider.name}.api_key`));
    for (const path of [...next]) {
      if (path.startsWith("providers.") && !allowedProviderSecrets.has(path)) next.delete(path);
    }
    state.configuredSecrets = next;
  }

  async function saveConfig() {
    if (!configDirty() || state.saving) return;
    const validationError = validateConfig();
    if (validationError) {
      showToast(validationError, "error", 5000);
      return;
    }
    const requiresRestart = restartChanges().length > 0;
    state.saving = true;
    updateChrome();
    try {
      const result = await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({
          revision: state.document.revision,
          config: stripSecrets(state.config),
          secret_changes: clone(state.secretChanges),
        }),
      });
      const savedDocument = result?.document || result;
      if (savedDocument?.config) {
        applySecretResults();
        applySettingsDocument({
          ...state.document,
          ...savedDocument,
          configured_secrets: savedDocument.configured_secrets || [...state.configuredSecrets],
          bootstrap: savedDocument.bootstrap || state.document.bootstrap,
          restart_required_paths: savedDocument.restart_required_paths || state.document.restart_required_paths,
        });
      } else {
        applySecretResults();
        state.document.revision = result?.revision ?? (Number(state.document.revision) + 1);
        state.baseline = clone(state.config);
        state.secretChanges = {};
      }
      showToast(requiresRestart ? "配置已保存，重启项将在 Bot 重启后生效" : "配置已保存", requiresRestart ? "warning" : "success", 5000);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        showToast("配置已被其他会话更新。请重新加载后再提交。", "warning", 6500);
      } else {
        showToast(error.message || "配置保存失败", "error", 6500);
      }
    } finally {
      state.saving = false;
      render();
    }
  }

  async function saveGroup(groupId) {
    const group = state.groups.find(item => String(item.id) === String(groupId));
    if (!group || !groupDirty(group) || state.groupSaving.has(String(groupId))) return;
    const invalid = [...content.querySelectorAll("[data-group-key], [data-template-buttons], [data-permission-control]")].find(
      control => String(control.dataset.groupId) === String(groupId) && !control.checkValidity(),
    );
    if (invalid) {
      invalid.reportValidity();
      showToast("请修正群组设置中标记的字段", "error", 5000);
      return;
    }
    const permissionConfig = group.settings.default_permissions;
    if (permissionConfig) {
      const emptyDays = permissionConfig.windows.find(window => !Array.isArray(window.days) || !window.days.length);
      if (emptyDays) {
        showToast(`权限时段「${emptyDays.name || emptyDays.id}」至少选择一个星期`, "error", 6000);
        return;
      }
      const emptyOverrides = permissionConfig.windows.find(window => !Object.keys(window.overrides || {}).length);
      if (emptyOverrides) {
        showToast(`权限时段「${emptyOverrides.name || emptyOverrides.id}」至少覆盖一项权限`, "error", 6000);
        return;
      }
    }
    const baseline = state.groupBaselines.get(String(group.id)) || {};
    if (!sameValue(group.settings.mimic_target_user_id, baseline.mimic_target_user_id)) {
      const hasExistingStyle = Boolean(
        baseline.mimic_target_user_id
        || baseline.mimic_target_user_name
        || baseline.mimic_profile_text
        || baseline.mimic_sample_count
        || baseline.mimic_distilled_at_count,
      );
      if (hasExistingStyle && !await askConfirmation(
        "更换风格目标",
        "更换或清除目标用户 ID 会重置已有名称、画像、采样计数和样本。",
      )) return;
    }
    state.groupSaving.add(String(groupId));
    renderContent();
    try {
      const changedSettings = Object.fromEntries(
        Object.entries(group.settings).filter(
          ([key, value]) => GROUP_EDITABLE_KEYS.has(key) && !sameValue(value, baseline[key]),
        ),
      );
      const result = await apiFetch(`/api/v1/groups/${encodeURIComponent(group.id)}/settings`, {
        method: "PUT",
        body: JSON.stringify({
          revision: group.revision,
          settings: clone(changedSettings),
        }),
      });
      const returnedSettings = result?.settings || result?.group?.settings;
      if (returnedSettings) group.settings = normalizeGroupSettings(returnedSettings);
      if (result?.group?.revision) group.revision = String(result.group.revision);
      state.groupTemplateButtonDrafts.delete(String(group.id));
      state.groupBaselines.set(String(group.id), clone(group.settings));
      const permissionApply = result?.permission_apply;
      if (permissionApply && !permissionApply.applied && !permissionApply.removed) {
        showToast(
          `${group.title || group.id} 的设置已保存，但 Telegram 权限下发失败；后台会自动重试${permissionApply.last_error ? `：${permissionApply.last_error}` : ""}`,
          "warning",
          7000,
        );
      } else {
        showToast(`${group.title || group.id} 的设置已保存`);
      }
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      showToast(
        conflict ? "群设置已发生变化，请刷新群组后重试" : error.message || "群组设置保存失败",
        conflict ? "warning" : "error",
        6000,
      );
    } finally {
      state.groupSaving.delete(String(groupId));
      renderContent();
      updateChrome();
    }
  }

  function providerReferences(name) {
    const references = [];
    for (const [roleName, meta] of Object.entries(ROLE_META)) {
      const role = state.config.models[roleName];
      if (role.provider === name) references.push(meta.label);
      if ((role.fallbacks || []).some(item => item.provider === name)) references.push(`${meta.label}回退链`);
    }
    return [...new Set(references)];
  }

  function renameProvider(index, oldName, rawName) {
    const provider = state.config.models.providers[index];
    if (!provider) return;
    const newName = String(rawName || "").trim().toLowerCase();
    if (!/^[a-z0-9_-]{1,64}$/.test(newName)) {
      provider.name = oldName;
      showToast("供应商名称只能包含字母、数字、下划线和连字符", "error");
      renderContent();
      return;
    }
    if (state.config.models.providers.some((item, itemIndex) => itemIndex !== index && item.name === newName)) {
      provider.name = oldName;
      showToast("供应商名称不能重复", "error");
      renderContent();
      return;
    }
    provider.name = newName;
    if (oldName && oldName !== newName) {
      for (const roleName of Object.keys(ROLE_META)) {
        const role = state.config.models[roleName];
        if (role.provider === oldName) role.provider = newName;
        for (const fallback of role.fallbacks || []) {
          if (fallback.provider === oldName) fallback.provider = newName;
        }
      }
      const oldSecretPath = `providers.${oldName}.api_key`;
      const newSecretPath = `providers.${newName}.api_key`;
      const pending = state.secretChanges[oldSecretPath];
      delete state.secretChanges[oldSecretPath];
      if (pending?.action === "replace") state.secretChanges[newSecretPath] = pending;
      if (state.configuredSecrets.has(oldSecretPath)) {
        showToast("供应商已重命名；请重新填写其 API Key", "warning", 5500);
      }
    }
    renderContent();
    updateChrome();
  }

  function readControlValue(target) {
    const kind = target.dataset.kind || "string";
    if (kind === "boolean") return target.checked;
    if (kind === "number") return target.value === "" ? null : Number(target.value);
    if (kind === "nonnegative-int") return target.value === "" ? 0 : Number(target.value);
    if (kind === "array") return target.value.split(",").map(value => value.trim()).filter(Boolean);
    if (kind === "nullable-boolean") {
      if (target.value === "") return null;
      return target.value === "true";
    }
    if (kind === "nullable-string") return target.value === "" ? null : target.value;
    if (kind === "nullable-int") return target.value === "" ? null : Number(target.value);
    if (kind === "lower-string") return target.value.trim().toLowerCase();
    return target.value;
  }

  function updateGroupCardState(target, group) {
    const card = target.closest("[data-group-card]");
    if (!card) return;
    const dirty = groupDirty(group);
    const button = card.querySelector('[data-action="save-group"]');
    if (button) button.disabled = !dirty;
    const headerActions = card.querySelector(".item-actions");
    let marker = headerActions?.querySelector(".group-save-state");
    if (dirty && !marker && headerActions) {
      marker = document.createElement("span");
      marker.className = "group-save-state";
      marker.textContent = "未保存";
      headerActions.prepend(marker);
    } else if (!dirty && marker) {
      marker.remove();
    }
  }

  function handlePermissionControl(target) {
    if (!target.matches("[data-permission-control]")) return false;
    const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
    const config = group?.settings?.default_permissions;
    if (!group || !config || state.groupSaving.has(String(group.id))) return true;
    if (target.dataset.permissionConfig) {
      config[target.dataset.permissionConfig] = target.type === "checkbox" ? target.checked : target.value;
    } else if (target.dataset.permissionBase) {
      config.base[target.dataset.permissionBase] = target.checked;
    } else {
      const index = Number(target.dataset.windowIndex);
      const window = config.windows[index];
      if (!window) return true;
      if (target.dataset.permissionWindowField) {
        const key = target.dataset.permissionWindowField;
        window[key] = target.dataset.windowKind === "boolean"
          ? target.checked
          : target.dataset.windowKind === "number"
            ? Number(target.value)
            : target.value;
      } else if (target.dataset.permissionWindowDay != null) {
        const day = Number(target.dataset.permissionWindowDay);
        const days = new Set(window.days || []);
        if (target.checked) days.add(day);
        else days.delete(day);
        window.days = [...days].sort((a, b) => a - b);
      } else if (target.dataset.permissionWindowOverride) {
        const key = target.dataset.permissionWindowOverride;
        if (target.value === "") delete window.overrides[key];
        else window.overrides[key] = target.value === "true";
      }
    }
    updateGroupCardState(target, group);
    return true;
  }

  // This control also has data-group-key. Keep input and blur/change on the
  // typed path so the generic group handler never replaces the array with text.
  function handleGroupTemplateButtonsControl(target) {
    if (!target.matches("[data-template-buttons]")) return false;
    const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
    if (!group || state.groupSaving.has(String(group.id))) return true;
    try {
      const buttons = parseTemplateButtonsText(target.value);
      group.settings[target.dataset.groupKey] = buttons;
      state.groupTemplateButtonDrafts.set(String(group.id), {
        raw: target.value,
        error: "",
        buttons: clone(buttons),
      });
      target.setCustomValidity("");
    } catch (error) {
      const message = error.message || "内联按钮格式无效";
      state.groupTemplateButtonDrafts.set(String(group.id), {
        raw: target.value,
        error: message,
        buttons: clone(group.settings[target.dataset.groupKey]),
      });
      target.setCustomValidity(message);
    }
    updateGroupCardState(target, group);
    return true;
  }

  content.addEventListener("input", event => {
    const target = event.target;
    const resourceForm = target.closest(RESOURCE_DRAFT_FORM_SELECTOR);
    if (resourceForm) {
      syncScheduledFormFields(resourceForm);
      captureResourceFormDraft(resourceForm);
    }
    if (handlePermissionControl(target)) return;
    if (handleGroupTemplateButtonsControl(target)) return;
    if (target.matches("[data-auto-delete-seconds]")) {
      const category = target.dataset.autoDeleteSeconds;
      const overrides = { ...(state.config.bot.auto_delete_category_seconds || {}) };
      const seconds = target.value === "" ? 0 : Math.max(0, Math.floor(Number(target.value) || 0));
      if (seconds > 0) overrides[category] = seconds;
      else delete overrides[category];
      state.config.bot.auto_delete_category_seconds = overrides;
      updateChrome();
      return;
    }
    if (target.matches("[data-path]")) {
      setPath(state.config, target.dataset.path, readControlValue(target));
      updateChrome();
      return;
    }
    if (target.matches("[data-secret-input]")) {
      const path = target.dataset.secretInput;
      const value = target.value;
      if (value) state.secretChanges[path] = { action: "replace", value };
      else delete state.secretChanges[path];
      const badge = target.closest(".secret-control")?.querySelector(".badge");
      if (badge) {
        badge.className = `badge ${value ? "warning" : state.configuredSecrets.has(path) ? "success" : ""}`.trim();
        badge.textContent = value ? "等待替换" : state.configuredSecrets.has(path) ? "已配置" : "未配置";
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-prompt-input]")) {
      const key = target.dataset.promptInput;
      state.config.prompts[key] = target.value;
      const meta = document.getElementById("prompt-meta");
      if (meta) meta.textContent = `${target.value.length.toLocaleString("zh-CN")} 字符`;
      const button = content.querySelector(`[data-action="select-prompt"][data-prompt-key="${CSS.escape(key)}"]`);
      if (button) {
        const existingDot = button.querySelector(".prompt-dot");
        if (promptDirty(key) && !existingDot) button.insertAdjacentHTML("beforeend", `<span class="prompt-dot" aria-label="已修改"></span>`);
        if (!promptDirty(key) && existingDot) existingDot.remove();
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-group-key]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      group.settings[target.dataset.groupKey] = readControlValue(target);
      updateGroupCardState(target, group);
      return;
    }
    if (target.id === "group-search") {
      state.groupSearch = target.value;
      applyGroupSearchFilter();
    }
  });

  content.addEventListener("change", event => {
    const target = event.target;
    const resourceForm = target.closest(RESOURCE_DRAFT_FORM_SELECTOR);
    if (resourceForm) {
      syncScheduledFormFields(resourceForm);
      captureResourceFormDraft(resourceForm);
    }
    if (handlePermissionControl(target)) return;
    if (handleGroupTemplateButtonsControl(target)) return;
    if (target.matches("[data-call-admin-target]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      const boxes = [...content.querySelectorAll(`[data-call-admin-target][data-group-id="${CSS.escape(String(group.id))}"]`)];
      const checked = boxes.filter(box => box.checked).map(box => Number(box.dataset.callAdminTarget));
      // All checked = "mention everyone" (stored as empty so future admins
      // are included automatically).
      group.settings.call_admin_targets = checked.length === boxes.length
        ? []
        : checked.sort((a, b) => a - b);
      updateGroupCardState(target, group);
      return;
    }
    if (target.matches("[data-access-admin-filter]")) {
      state.accessAdminGroup = target.value;
      state.listPages.set("access:admins", 1);
      renderContent();
      return;
    }
    if (target.matches("[data-auto-delete-category]")) {
      const selected = [...content.querySelectorAll("[data-auto-delete-category]:checked")]
        .map(control => control.dataset.autoDeleteCategory);
      state.config.bot.auto_delete_categories = selected;
      const category = target.dataset.autoDeleteCategory;
      const modeSelect = content.querySelector(
        `[data-auto-delete-mode="${CSS.escape(category)}"]`,
      );
      if (modeSelect) modeSelect.disabled = !target.checked;
      const secondsInput = content.querySelector(
        `[data-auto-delete-seconds="${CSS.escape(category)}"]`,
      );
      if (secondsInput) {
        secondsInput.disabled = !target.checked || (modeSelect && modeSelect.value === "button");
      }
      updateChrome();
      return;
    }
    if (target.matches("[data-auto-delete-mode]")) {
      const category = target.dataset.autoDeleteMode;
      const modes = { ...(state.config.bot.auto_delete_category_mode || {}) };
      if (target.value === "button") modes[category] = "button";
      else delete modes[category];
      state.config.bot.auto_delete_category_mode = modes;
      const secondsInput = content.querySelector(
        `[data-auto-delete-seconds="${CSS.escape(category)}"]`,
      );
      if (secondsInput) secondsInput.disabled = target.value === "button" || target.disabled;
      updateChrome();
      return;
    }
    if (target.matches("[data-auto-delete-seconds]")) {
      const category = target.dataset.autoDeleteSeconds;
      const overrides = { ...(state.config.bot.auto_delete_category_seconds || {}) };
      const seconds = target.value === "" ? 0 : Math.max(0, Math.floor(Number(target.value) || 0));
      if (seconds > 0) overrides[category] = seconds;
      else delete overrides[category];
      state.config.bot.auto_delete_category_seconds = overrides;
      updateChrome();
      return;
    }
    if (target.matches("[data-path]")) {
      setPath(state.config, target.dataset.path, readControlValue(target));
      updateChrome();
    }
    if (target.matches("[data-group-key]")) {
      const group = state.groups.find(item => String(item.id) === String(target.dataset.groupId));
      if (!group || state.groupSaving.has(String(group.id))) return;
      group.settings[target.dataset.groupKey] = readControlValue(target);
      updateGroupCardState(target, group);
    }
  });

  content.addEventListener("focusout", event => {
    const target = event.target;
    if (target.matches("[data-provider-index]")) {
      renameProvider(Number(target.dataset.providerIndex), target.dataset.providerOldName, target.value);
    }
  });

  content.addEventListener("click", async event => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = button.dataset.action;
    if (action === "retry-load") {
      loadAll();
      return;
    }
    if (action === "list-page") {
      state.listPages.set(button.dataset.listKey, Number(button.dataset.page) || 1);
      renderContent();
      return;
    }
    if (action === "add-provider") {
      const existing = new Set(state.config.models.providers.map(item => item.name));
      let suffix = state.config.models.providers.length + 1;
      let name = `provider${suffix}`;
      while (existing.has(name)) name = `provider${++suffix}`;
      state.config.models.providers.push({
        name,
        provider: "openai",
        api_key: "",
        api_base: "",
        stream: false,
        chat_endpoint: "auto",
      });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-provider") {
      const index = Number(button.dataset.index);
      const provider = state.config.models.providers[index];
      if (!provider) return;
      if (state.config.models.providers.length <= 1) {
        showToast("至少需要保留一个模型供应商", "error");
        return;
      }
      const references = providerReferences(provider.name);
      if (references.length) {
        showToast(`请先移除以下引用：${references.join("、")}`, "warning", 6000);
        return;
      }
      if (!await askConfirmation("删除供应商", `确认删除 ${provider.name}？对应的已保存密钥也会在保存配置时清除。`)) return;
      state.config.models.providers.splice(index, 1);
      delete state.secretChanges[`providers.${provider.name}.api_key`];
      renderContent();
      updateChrome();
      return;
    }
    if (action === "add-fallback") {
      const role = state.config.models[button.dataset.role];
      const provider = state.config.models.providers[0]?.name || "";
      role.fallbacks.push({ provider, model: "" });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-fallback") {
      state.config.models[button.dataset.role].fallbacks.splice(Number(button.dataset.index), 1);
      renderContent();
      updateChrome();
      return;
    }
    if (action === "secret-clear") {
      const path = button.dataset.secretPath;
      state.secretChanges[path] = { action: "clear" };
      renderContent();
      updateChrome();
      return;
    }
    if (action === "secret-undo") {
      delete state.secretChanges[button.dataset.secretPath];
      renderContent();
      updateChrome();
      return;
    }
    if (action === "select-prompt") {
      const promptList = content.querySelector(".prompt-list");
      const promptScrollLeft = promptList?.scrollLeft || 0;
      state.promptKey = button.dataset.promptKey;
      renderContent();
      const nextPromptList = content.querySelector(".prompt-list");
      if (nextPromptList) {
        nextPromptList.scrollLeft = promptScrollLeft;
        nextPromptList.querySelector(".prompt-button.active")?.scrollIntoView({
          block: "nearest",
          inline: "nearest",
        });
      }
      return;
    }
    if (action === "clear-prompt") {
      if (!await askConfirmation("使用内置 Prompt", "清空此文本后，运行时将使用项目内置的默认 Prompt。")) return;
      state.config.prompts[button.dataset.promptKey] = "";
      renderContent();
      updateChrome();
      return;
    }
    if (action === "load-call-admin-targets") {
      loadCallAdminTargets(button.dataset.groupId);
      return;
    }
    if (action === "load-group-permissions") {
      loadGroupPermissions(button.dataset.groupId);
      return;
    }
    if (action === "add-permission-window") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const config = group?.settings?.default_permissions;
      if (!group || !config || state.groupSaving.has(String(group.id))) return;
      const ids = new Set(config.windows.map(window => window.id));
      let id = `window_${Date.now().toString(36)}`;
      while (ids.has(id)) id += "x";
      const firstField = state.permissionFields.find(field => field.key === "can_send_photos")?.key
        || state.permissionFields[0]?.key;
      config.windows.push({
        id,
        name: "夜间模式",
        enabled: true,
        start: "23:00",
        end: "07:00",
        days: [0, 1, 2, 3, 4, 5, 6],
        priority: 0,
        overrides: firstField ? { [firstField]: false } : {},
      });
      renderContent();
      updateChrome();
      return;
    }
    if (action === "remove-permission-window") {
      const group = state.groups.find(item => String(item.id) === String(button.dataset.groupId));
      const config = group?.settings?.default_permissions;
      if (!group || !config || state.groupSaving.has(String(group.id))) return;
      config.windows.splice(Number(button.dataset.windowIndex), 1);
      renderContent();
      updateChrome();
      return;
    }
    if (action === "trigger-patrol") {
      const groupId = button.dataset.groupId;
      if (!await askConfirmation(
        "手动触发巡检",
        "将立即分批检查该群所有已知成员的名字和简介；违规者会被禁言并要求真人质询。确认开始？",
      )) return;
      button.disabled = true;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/patrol`, { method: "POST" });
        showToast("巡检已开始，将在后台分批执行");
      } catch (error) {
        showToast(error.message || "巡检启动失败", "error", 6000);
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (action === "save-group") {
      saveGroup(button.dataset.groupId);
      return;
    }
    if (action === "load-group-resources") {
      loadGroupResources(button.dataset.groupId);
      return;
    }
    if (action === "load-access") {
      loadAccess();
      return;
    }
    if (action === "delete-access") {
      const type = button.dataset.accessType;
      const id = button.dataset.accessId;
      let url = `/api/v1/${type}/${encodeURIComponent(id)}`;
      if (type === "admins") {
        const [groupId, userId] = String(id).split(":", 2);
        url = `/api/v1/groups/${encodeURIComponent(groupId)}/admins/${encodeURIComponent(userId)}`;
      }
      if (!await askConfirmation("确认操作", type === "global-bans" ? "确认解封该用户？" : "确认移除这条授权？")) return;
      try {
        await apiFetch(url, { method: "DELETE" });
        await loadAccess();
        await reloadGroups();
        showToast("操作已完成");
      } catch (error) {
        showToast(error.message || "操作失败", "error");
      }
      return;
    }
    if (action === "delete-group-resource") {
      const groupId = button.dataset.groupId;
      const type = button.dataset.resourceType;
      const id = button.dataset.resourceId;
      if (!await askConfirmation("删除记录", "确认删除这条群管理记录？")) return;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/${type}/${encodeURIComponent(id)}`, { method: "DELETE" });
        const deletedForm = button.closest(RESOURCE_DRAFT_FORM_SELECTOR);
        if (deletedForm) markResourceFormClean(deletedForm);
        await loadGroupResources(groupId);
        showToast("已删除");
      } catch (error) {
        showToast(error.message || "删除失败", "error");
      }
      return;
    }
    if (action === "edit-group-resource") {
      const groupId = button.dataset.groupId;
      const type = button.dataset.resourceType;
      const resource = state.groupResources.get(String(groupId));
      const item = (resource?.[type] || []).find(row => String(row.id) === String(button.dataset.resourceId));
      if (!item) return;
      const nextValue = window.prompt(
        type === "rules" ? "修改群规内容" : "修改永久记忆",
        type === "rules" ? item.pattern : item.content,
      );
      if (nextValue == null || !nextValue.trim()) return;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/${type}/${encodeURIComponent(item.id)}`, {
          method: "PATCH",
          body: JSON.stringify(type === "rules" ? { pattern: nextValue } : { content: nextValue }),
        });
        await loadGroupResources(groupId);
        showToast("已更新");
      } catch (error) {
        showToast(error.message || "更新失败", "error");
      }
      return;
    }
    if (action === "reload-groups") {
      if (state.groupSaving.size) return;
      captureResourceFormDrafts();
      if ((anyGroupDirty() || anyResourceFormDirty()) && !await askConfirmation(
        "刷新群组",
        "刷新会丢弃所有未保存的群组设置和群管理表单草稿。",
      )) return;
      state.groupTemplateButtonDrafts.clear();
      clearResourceFormDrafts();
      reloadGroups();
    }
  });

  const ENTRY_FORM_FIELDS = {
    "keyword-replies": {
      strings: ["keyword", "match_type", "reply_text"],
      booleans: ["pin_message", "auto_delete", "enabled"],
      numbers: [],
    },
    "scheduled-messages": {
      strings: ["text", "schedule_type", "schedule_time"],
      booleans: ["pin_message", "unpin_previous", "auto_delete", "enabled"],
      numbers: ["interval_minutes"],
    },
  };

  function readEntryFormValues(form, type, { includeEnabled }) {
    const spec = ENTRY_FORM_FIELDS[type];
    const values = {};
    for (const name of spec.strings) {
      const control = form.elements[name];
      if (!control || control.disabled) continue;
      values[name] = control.value;
    }
    for (const name of spec.numbers) {
      const control = form.elements[name];
      if (control && !control.disabled) values[name] = Number(control.value);
    }
    for (const name of spec.booleans) {
      if (name === "enabled" && !includeEnabled) continue;
      const control = form.elements[name];
      if (control) values[name] = control.checked;
    }
    const buttonsControl = form.elements.buttons_text;
    if (buttonsControl) values.buttons = parseTemplateButtonsText(buttonsControl.value);
    return values;
  }

  content.addEventListener("submit", async event => {
    const entryEditForm = event.target.closest("[data-entry-edit-form]");
    if (entryEditForm) {
      event.preventDefault();
      syncScheduledFormFields(entryEditForm);
      captureResourceFormDraft(entryEditForm);
      if (!entryEditForm.reportValidity()) return;
      const type = entryEditForm.dataset.entryEditForm;
      let values;
      try {
        values = readEntryFormValues(entryEditForm, type, { includeEnabled: true });
      } catch (error) {
        showToast(error.message || "内联按钮格式无效", "error", 6000);
        return;
      }
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(entryEditForm.dataset.groupId)}/${type}/${encodeURIComponent(entryEditForm.dataset.entryId)}`, {
          method: "PATCH",
          body: JSON.stringify(values),
        });
        markResourceFormClean(entryEditForm);
        await loadGroupResources(entryEditForm.dataset.groupId);
        showToast("已更新");
      } catch (error) {
        showToast(error.message || "更新失败", "error");
      }
      return;
    }
    const ruleEditForm = event.target.closest("[data-rule-edit-form]");
    if (ruleEditForm) {
      event.preventDefault();
      captureResourceFormDraft(ruleEditForm);
      if (!ruleEditForm.reportValidity()) return;
      const values = Object.fromEntries(new FormData(ruleEditForm).entries());
      values.enabled = ruleEditForm.elements.enabled.checked;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(ruleEditForm.dataset.groupId)}/rules/${encodeURIComponent(ruleEditForm.dataset.ruleId)}`, {
          method: "PATCH",
          body: JSON.stringify(values),
        });
        markResourceFormClean(ruleEditForm);
        await loadGroupResources(ruleEditForm.dataset.groupId);
        showToast("群规已更新");
      } catch (error) {
        showToast(error.message || "群规更新失败", "error");
      }
      return;
    }
    const memoryEditForm = event.target.closest("[data-memory-edit-form]");
    if (memoryEditForm) {
      event.preventDefault();
      captureResourceFormDraft(memoryEditForm);
      if (!memoryEditForm.reportValidity()) return;
      try {
        await apiFetch(`/api/v1/groups/${encodeURIComponent(memoryEditForm.dataset.groupId)}/memories/${encodeURIComponent(memoryEditForm.dataset.memoryId)}`, {
          method: "PATCH",
          body: JSON.stringify({ content: memoryEditForm.elements.content.value }),
        });
        markResourceFormClean(memoryEditForm);
        await loadGroupResources(memoryEditForm.dataset.groupId);
        showToast("永久记忆已更新");
      } catch (error) {
        showToast(error.message || "永久记忆更新失败", "error");
      }
      return;
    }
    const accessForm = event.target.closest("[data-access-form]");
    if (accessForm) {
      event.preventDefault();
      if (!accessForm.reportValidity()) return;
      const type = accessForm.dataset.accessForm;
      const values = Object.fromEntries(new FormData(accessForm).entries());
      for (const key of ["group_id", "user_id"]) if (values[key] != null) values[key] = Number(values[key]);
      let url = `/api/v1/${type}`;
      if (type === "admins") {
        url = `/api/v1/groups/${encodeURIComponent(values.group_id)}/admins`;
        delete values.group_id;
      }
      try {
        await apiFetch(url, { method: "POST", body: JSON.stringify(values) });
        await loadAccess();
        await reloadGroups();
        showToast("操作已完成");
      } catch (error) {
        showToast(error.message || "操作失败", "error");
      }
      return;
    }
    const form = event.target.closest("[data-resource-form]");
    if (!form) return;
    event.preventDefault();
    syncScheduledFormFields(form);
    captureResourceFormDraft(form);
    if (!form.reportValidity()) return;
    const groupId = form.dataset.groupId;
    const type = form.dataset.resourceForm;
    let values;
    try {
      values = ENTRY_FORM_FIELDS[type]
        ? readEntryFormValues(form, type, { includeEnabled: false })
        : Object.fromEntries(new FormData(form).entries());
    } catch (error) {
      showToast(error.message || "内联按钮格式无效", "error", 6000);
      return;
    }
    if (values.user_id != null) values.user_id = Number(values.user_id);
    try {
      await apiFetch(`/api/v1/groups/${encodeURIComponent(groupId)}/${type}`, {
        method: "POST",
        body: JSON.stringify(values),
      });
      markResourceFormClean(form);
      await loadGroupResources(groupId);
      showToast("已新增");
    } catch (error) {
      showToast(error.message || "新增失败", "error");
    }
  });

  function switchTab(tab) {
    if (!navItems().some(item => item.id === tab)) return;
    state.activeTab = tab;
    app.classList.remove("sidebar-open");
    sidebar.classList.remove("mobile-open");
    updateChrome();
    renderContent({ resetScroll: true });
    const activeMobile = mobileNav.querySelector(`[data-nav="${CSS.escape(tab)}"]`);
    if (activeMobile) {
      const left = activeMobile.offsetLeft - (mobileNav.clientWidth - activeMobile.offsetWidth) / 2;
      mobileNav.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
    }
  }

  document.addEventListener("click", event => {
    const nav = event.target.closest("[data-nav]");
    if (nav) switchTab(nav.dataset.nav);
  });

  sidebarToggle.addEventListener("click", () => {
    const open = !sidebar.classList.contains("mobile-open");
    sidebar.classList.toggle("mobile-open", open);
    app.classList.toggle("sidebar-open", open);
  });

  app.addEventListener("click", event => {
    if (event.target === app && app.classList.contains("sidebar-open")) {
      app.classList.remove("sidebar-open");
      sidebar.classList.remove("mobile-open");
    }
  });

  saveButton.addEventListener("click", saveConfig);

  reloadButton.addEventListener("click", async () => {
    if (state.groupSaving.size) return;
    captureResourceFormDrafts();
    if ((configDirty() || anyGroupDirty() || anyResourceFormDirty()) && !await askConfirmation(
      "重新加载",
      "重新加载会丢弃所有未保存的配置、群组设置和群管理表单草稿。",
    )) return;
    state.groupTemplateButtonDrafts.clear();
    clearResourceFormDrafts();
    loadAll();
  });

  window.addEventListener("beforeunload", event => {
    captureResourceFormDrafts();
    if (!configDirty() && !anyGroupDirty() && !anyResourceFormDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  telegramInit();
  updateNavigation();
  refreshIcons();
  loadAll({ keepTab: false });
})();
