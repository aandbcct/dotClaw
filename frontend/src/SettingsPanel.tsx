import { useState } from "react";
import type { RuntimeSettings } from "./types";
import { Dialog } from "./Dialog";

type Section = "models" | "general" | "tools" | "skills" | "mcp" | "about";
interface SettingsPanelProps {
  settings: RuntimeSettings | null; loading: boolean; busyKey: string | null; error?: string; onRetry?: () => void;
  onClose: () => void; onToggleTool: (name: string, enabled: boolean) => void;
  onToggleSkill: (name: string, enabled: boolean) => void; onReconnectMcp: () => void;
}
const SECTIONS: Array<[Section, string]> = [["models", "模型"], ["general", "运行设置"], ["tools", "工具"], ["skills", "Skills"], ["mcp", "MCP"], ["about", "关于"]];

/** 配置只读项与实际可用开关明确区分，读取失败留在弹层内处理。 */
export function SettingsPanel({ settings, loading, busyKey, error, onRetry, onClose, onToggleTool, onToggleSkill, onReconnectMcp }: SettingsPanelProps) {
  const [section, setSection] = useState<Section>("models");
  return <Dialog title="设置" className="settings-panel" onClose={onClose}>
    <div className="settings-layout">
      <nav aria-label="设置分类">{SECTIONS.map(([id, label]) => <button className={section === id ? "active" : ""} type="button" key={id} onClick={() => setSection(id)}>{label}</button>)}</nav>
      <div className="settings-content">
        {error && <div className="inline-error" role="alert">{error}{onRetry && <button type="button" onClick={onRetry}>重新读取</button>}</div>}
        {loading ? <p className="settings-loading">正在读取本地配置…</p> : section === "about" ? <section><Heading title="关于 dotClaw" note="本地聊天工作区" /><p>会话与运行记录存储在当前后端所在的本机。使用远程模型或网络工具时，请求内容仍会发送到相应服务。</p></section> : settings ? <>
          {section === "models" && <ModelSection settings={settings} />}
          {section === "general" && <GeneralSection settings={settings} />}
          {section === "tools" && <ToggleList key="tools" title="工具" note="开关影响后续运行；审批和访问策略仍然生效。" items={settings.tools.map((item) => ({ ...item, meta: `${sourceLabel(item.source)}${item.needs_approval ? " · 需要审批" : ""}` }))} busy={busyKey !== null} onToggle={onToggleTool} />}
          {section === "skills" && <ToggleList key="skills" title="Skills" note="控制后续技能匹配与提示词注入。" items={settings.skills.map((item) => ({ ...item, meta: item.always_load ? "始终加载" : "按需匹配" }))} busy={busyKey !== null} onToggle={onToggleSkill} />}
          {section === "mcp" && <McpSection settings={settings} busy={busyKey !== null} onReconnect={onReconnectMcp} />}
        </> : !error && <p className="settings-empty">未读取到配置</p>}
      </div>
    </div>
  </Dialog>;
}
function ModelSection({ settings }: { settings: RuntimeSettings }) {
  return <section><Heading title="模型" readOnly note="修改本地 model_router_config.yaml 并重启后生效；密钥不回传浏览器。" />
    <div className="settings-cards">{settings.models.map((model) => <details className="settings-card model-row" key={model.name}>
      <summary><div className="settings-card-title"><div><strong>{model.name}</strong><span>{model.provider} · {model.model_id}</span></div><div className="model-labels"><span>{model.capabilities.includes("embedding") ? "向量模型" : "对话模型"}</span>{model.preferred && <span>默认</span>}</div></div></summary>
      <div className="model-facts"><span>{model.status === "active" ? "已启用" : "已停用"}</span><span>上下文 {model.context_window.toLocaleString()} tokens</span><span>推理模式 {model.reasoning_mode}</span><span>{model.api_key_configured ? "密钥已配置" : "密钥未配置"}</span></div>
      <div className="tag-row">{model.capabilities.map((item) => <span key={item}>{item}</span>)}</div>
    </details>)}</div>
  </section>;
}
function GeneralSection({ settings }: { settings: RuntimeSettings }) {
  const rows = [
    ["流式输出配置", settings.general.stream ? "开启" : "关闭"],
    ["上下文上限", `${settings.general.max_context_tokens.toLocaleString()} tokens`],
    ["保留最近消息配置", `${settings.general.keep_recent_messages} 条`],
    ["工具超时配置", `${settings.general.exec_timeout} 秒`],
    ["调度器配置", settings.general.scheduler_enabled ? "开启" : "关闭"],
  ];
  return <section><Heading title="运行设置" readOnly note="修改本地 config.yaml 并重启。以下为已加载配置，不代表所有旧参数均由当前执行链消费。" />
    <div className="setting-rows">{rows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <p className="muted">当前 Web 输出使用流式传输；消息保留、默认超时及调度器参数尚未完整进入当前主链。</p>
  </section>;
}
function ToggleList({ title, note, items, busy, onToggle }: { title: string; note: string; items: Array<{ name: string; description: string; enabled: boolean; meta: string }>; busy: boolean; onToggle: (name: string, enabled: boolean) => void }) {
  const [query, setQuery] = useState("");
  const filtered = items.filter((item) => `${item.name} ${item.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <section><Heading title={title} note={note} /><input className="settings-filter" type="search" aria-label={`过滤${title}`} placeholder="按名称或描述过滤…" value={query} onChange={(event) => setQuery(event.target.value)} />
    <div className="toggle-list">{filtered.map((item) => <article key={item.name}><details className="toggle-copy"><summary><strong>{item.name}</strong><span className="toggle-description">{item.description || "暂无描述"}</span></summary><p>{item.description}</p><small>{item.meta}</small></details>
      <label className="switch"><input type="checkbox" aria-label={`${item.enabled ? "关闭" : "开启"} ${item.name}`} checked={item.enabled} disabled={busy} onChange={() => onToggle(item.name, !item.enabled)} /><span /></label>
    </article>)}</div>{!filtered.length && <p className="muted">没有匹配项目</p>}
  </section>;
}
function McpSection({ settings, busy, onReconnect }: { settings: RuntimeSettings; busy: boolean; onReconnect: () => void }) {
  return <section><Heading title="MCP 连接" note="连接状态来自当前后端进程；本轮不提供 server 增删改。" />
    <div className="mcp-summary"><strong>{settings.mcp_enabled ? "MCP 已启用" : "MCP 已关闭"}</strong><button type="button" disabled={busy || !settings.mcp_servers.length} onClick={onReconnect}>{busy ? "正在重连…" : "重新连接"}</button></div>
    {!settings.mcp_servers.length ? <p className="settings-empty">当前没有配置 MCP server</p> : settings.mcp_servers.map((server) => <article className="settings-card" key={server.name}>
      <div className="settings-card-title"><div><strong>{server.name}</strong><span>{server.transport} · {server.endpoint || "未设置地址"}</span></div><span className="status-pill">{server.state === "connected" ? "已连接" : server.state === "failed" ? "连接失败" : server.state === "starting" ? "正在连接" : "未连接"}</span></div>
      <div className="model-facts"><span>{server.tool_count} 个工具</span>{server.detail && <span>{server.detail}</span>}</div>
    </article>)}
  </section>;
}
function Heading({ title, note, readOnly = false }: { title: string; note: string; readOnly?: boolean }) {
  return <div className="settings-section-heading"><h3>{title}{readOnly && <span className="tag">只读</span>}</h3><p>{note}</p></div>;
}
function sourceLabel(source: string): string { return ({ builtin: "内置", mcp: "MCP", skill: "Skill", custom: "自定义" } as Record<string, string>)[source] || source; }
