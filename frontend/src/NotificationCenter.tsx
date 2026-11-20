import { useCallback, useEffect, useRef, useState } from "react";
import { getNotification, listNotifications, markNotificationRead } from "./api";
import "./notification-workspace.css";
import type { NotificationItem } from "./types";

type Permission = "unsupported" | "default" | "denied" | "granted";

function timeLabel(value: string) {
  return new Date(value).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
}

export function formatNotificationBody(kind: NotificationItem["kind"], body: string) {
  if (kind !== "reminder") return body;
  const match = /^提醒时间：([^；]+)；事务截止：(\d{4}-\d{2}-\d{2})$/.exec(body);
  if (!match) return body;
  const reminderDate = new Date(match[1]);
  if (!Number.isFinite(reminderDate.getTime())) return body;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const localTime = reminderDate.toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" });
  return `提醒时间：${localTime}（浏览器当地时间，${zone}）；事务截止：${match[2]}`;
}

function taskStatus(status: string | null) {
  if (status === "pending") return "待办";
  if (status === "in_progress") return "进行中";
  if (status === "completed") return "已完成";
  if (status === "cancelled") return "已取消";
  return "事务已删除";
}

export function NotificationCenter({ onOpenTask, onOpenSummary, onRefresh, onOpenNavigation, revision, unreadCount, connectionError, permission, permissionPending, permissionError, onEnableSystemNotifications }: {
  onOpenNavigation: () => void;
  onOpenTask: (taskId: string) => void;
  onOpenSummary: (notificationId: number) => void;
  onRefresh: () => Promise<unknown>;
  revision: number;
  unreadCount: number;
  connectionError: string;
  permission: Permission;
  permissionPending: boolean;
  permissionError: string;
  onEnableSystemNotifications: () => void;
}) {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [selected, setSelected] = useState<NotificationItem | null>(null);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState("");
  const [actionPending, setActionPending] = useState<number | null>(null);
  const requestId = useRef(0);
  const selectedRequestId = useRef(0);
  const filterRef = useRef(unreadOnly);
  filterRef.current = unreadOnly;

  const loadLatest = useCallback(async (showLoading: boolean) => {
    const id = ++requestId.current;
    if (showLoading) setLoading(true);
    try {
      const page = await listNotifications({ unread_only: filterRef.current });
      if (id !== requestId.current || filterRef.current !== unreadOnly) return;
      setItems((current) => {
        const merged = new Map<number, NotificationItem>();
        for (const item of page.items) merged.set(item.id, item);
        for (const item of current) if (!merged.has(item.id)) merged.set(item.id, item);
        return [...merged.values()].sort((a, b) => b.id - a.id);
      });
      setNext((current) => current ?? page.next_before_id);
      setError("");
    } catch (reason) {
      if (id === requestId.current) setError(reason instanceof Error ? reason.message : "读取通知失败");
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [unreadOnly]);

  useEffect(() => {
    requestId.current += 1;
    setItems([]);
    setNext(null);
    setSelected(null);
    setError("");
    void loadLatest(true);
  }, [unreadOnly, loadLatest]);

  useEffect(() => {
    if (revision > 0) void loadLatest(false);
  }, [revision, loadLatest]);

  async function loadOlder() {
    if (next === null || loadingOlder) return;
    const cursor = next;
    const filter = unreadOnly;
    const id = requestId.current;
    setLoadingOlder(true);
    try {
      const page = await listNotifications({ unread_only: filter, before_id: cursor });
      if (id !== requestId.current || filter !== filterRef.current) return;
      setItems((current) => {
        const merged = new Map(current.map((item) => [item.id, item]));
        for (const item of page.items) merged.set(item.id, item);
        return [...merged.values()].sort((a, b) => b.id - a.id);
      });
      setNext(page.next_before_id);
      setError("");
    } catch (reason) { if (id === requestId.current) setError(reason instanceof Error ? reason.message : "读取更早通知失败"); }
    finally { setLoadingOlder(false); }
  }

  async function open(item: NotificationItem) {
    const id = ++selectedRequestId.current;
    setSelected(item);
    setActionPending(item.id);
    setError("");
    try {
      const detail = await getNotification(item.id);
      if (id !== selectedRequestId.current) return;
      setSelected(detail);
      if (!detail.read_at) {
        const read = await markNotificationRead(item.id);
        if (id !== selectedRequestId.current) return;
        setSelected(read);
        setItems((current) => unreadOnly ? current.filter((entry) => entry.id !== read.id) : current.map((entry) => entry.id === read.id ? read : entry));
        await onRefresh();
      }
    } catch (reason) { if (id === selectedRequestId.current) setError(reason instanceof Error ? reason.message : "读取通知失败"); }
    finally { if (id === selectedRequestId.current) setActionPending(null); }
  }

  const permissionText: Record<Permission, string> = {
    unsupported: "当前环境不支持系统通知",
    default: "系统通知尚未启用",
    denied: "浏览器已拒绝通知权限，可在浏览器设置中更改",
    granted: "系统通知已启用",
  };
  const closeDetail = () => { selectedRequestId.current += 1; setSelected(null); setActionPending(null); };

  return <section className={`notification-workspace ${selected ? "notification-workspace--detail" : ""}`}>
    <header className="notification-workspace__header">
      <button className="menu-button" aria-label="打开导航" onClick={onOpenNavigation}>☰</button>
      <div><h1>通知中心</h1><p>{unreadCount} 条未读通知</p></div>
      <button type="button" onClick={() => void loadLatest(true)} disabled={loading} aria-label="刷新通知">{loading ? "正在刷新…" : "刷新"}</button>
      <button type="button" aria-pressed={unreadOnly} onClick={() => setUnreadOnly((value) => !value)}>{unreadOnly ? "显示全部" : "仅未读"}</button>
    </header>
    <aside className="notification-workspace__status" aria-label="通知状态">
      <span>{connectionError ? `通知连接中断：${connectionError}` : permissionText[permission]}</span>
      {permission === "default" && <button type="button" onClick={onEnableSystemNotifications} disabled={permissionPending}>{permissionPending ? "正在请求权限…" : "启用系统通知"}</button>}
      {permissionError && <span role="alert">{permissionError}</span>}
      {connectionError && <button type="button" onClick={() => void loadLatest(true)}>重试读取</button>}
    </aside>
    <div className="notification-workspace__body">
      <div className="notification-workspace__list-pane" aria-label="通知列表">
        {error && <p className="notification-workspace__error" role="alert">{error}<button type="button" onClick={() => void loadLatest(true)}>重试</button></p>}
        {loading && !items.length && <p role="status">正在读取通知…</p>}
        {!loading && !items.length && !error && <p className="notification-workspace__empty">{unreadOnly ? "没有未读通知" : "暂无通知"}</p>}
        <ul className="notification-workspace__list">
          {items.map((item) => <li key={item.id} className={!item.read_at ? "is-unread" : ""}>
            <button type="button" aria-current={selected?.id === item.id ? "true" : undefined} onClick={() => void open(item)} disabled={actionPending === item.id}>
              <span className="notification-workspace__item-heading"><strong>{item.title}</strong><span>{item.read_at ? "已读" : "未读"}</span></span>
              <time dateTime={item.created_at}>{timeLabel(item.created_at)}</time>
              <span className="notification-workspace__summary">{item.body}</span>
              {actionPending === item.id && <small role="status">正在打开…</small>}
            </button>
          </li>)}
        </ul>
        {next !== null && <button className="notification-workspace__older" type="button" onClick={() => void loadOlder()} disabled={loadingOlder}>{loadingOlder ? "正在加载…" : "加载更早通知"}</button>}
      </div>
      <article className="notification-workspace__detail" aria-label="通知详情">
        {selected ? <>
          <button className="notification-workspace__back" type="button" onClick={closeDetail}>返回通知列表</button>
          <h2>{selected.title}</h2><time dateTime={selected.created_at}>{timeLabel(selected.created_at)}</time><p className="notification-workspace__body-copy">{formatNotificationBody(selected.kind, selected.body)}</p>
          {actionPending === selected.id && <p role="status">正在读取详情…</p>}
          {selected.members.length > 0 && <section><h3>相关事务</h3><ul>{selected.members.map((member) => <li key={member.task_id}><button type="button" onClick={() => onOpenTask(member.task_id)}>{member.title_snapshot}</button><span>{member.missed ? "错过提醒" : "提醒"}{member.overdue ? " · 当时逾期" : ""} · 当前状态：{taskStatus(member.current_status)}</span></li>)}</ul></section>}
          {selected.kind === "recovery_summary" && <button type="button" onClick={() => onOpenSummary(selected.id)}>查看摘要事务</button>}
          {selected.deliveries.length > 0 && <section><h3>系统通知记录</h3><ul>{selected.deliveries.map((entry) => <li key={entry.claim_id}>{entry.status === "accepted" ? "已交给浏览器" : entry.status === "failed" ? `投递失败：${entry.error}` : "结果未确认"}</li>)}</ul></section>}
        </> : <p className="notification-workspace__placeholder">选择一条通知查看详情</p>}
      </article>
    </div>
  </section>;
}
