import { useEffect, useState, type ReactNode } from "react";
import { getNotification, listNotifications, markNotificationRead } from "./api";
import type { NotificationItem } from "./types";

export function NotificationCenter({ onOpenTask, onOpenSummary, onRefresh, onOpenNavigation, systemNotifications }: { onOpenNavigation: () => void; systemNotifications: ReactNode; onOpenTask: (taskId: string) => void; onOpenSummary: (notificationId: number) => void; onRefresh: () => Promise<unknown> }) {
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [selected, setSelected] = useState<NotificationItem | null>(null);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [beforeId, setBeforeId] = useState<number | undefined>();
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState("");
  async function load(older = false) { try { const page = await listNotifications({ unread_only: unreadOnly, before_id: older ? beforeId : undefined }); setItems((current) => older ? [...current, ...page.items] : page.items); setBeforeId(page.next_before_id || undefined); setNext(page.next_before_id); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "读取通知失败"); } }
  useEffect(() => { void load(); }, [unreadOnly]);
  async function open(item: NotificationItem) { try { const detail = await getNotification(item.id); setSelected(detail); if (!detail.read_at) { await markNotificationRead(item.id); await onRefresh(); await load(); } } catch (reason) { setError(reason instanceof Error ? reason.message : "读取通知失败"); } }
  return <section className="notification-center"><header><button className="menu-button" aria-label="打开导航" onClick={onOpenNavigation}>☰</button><h1>通知中心</h1><button onClick={() => void load()}>刷新</button><button onClick={() => setUnreadOnly((value) => !value)} aria-pressed={unreadOnly}>{unreadOnly ? "显示全部" : "仅未读"}</button></header>
    {systemNotifications}{error && <p role="alert">{error}</p>}{!items.length ? <p>暂无通知</p> : <ul className="notification-list">{items.map((item) => <li key={item.id} className={item.read_at ? "" : "notification-unread"}><button onClick={() => void open(item)}><strong>{item.title}</strong><span>{item.read_at ? "已读" : "未读"}</span><small>{item.body}</small></button></li>)}</ul>}{next && <button onClick={() => void load(true)}>加载更早通知</button>}
    {selected && <article className="notification-detail"><h2>{selected.title}</h2><p>{selected.body}</p><ul>{selected.members.map((member) => <li key={member.task_id}><button onClick={() => onOpenTask(member.task_id)}>{member.title_snapshot}</button><span>{member.missed ? "错过提醒" : "提醒"}{member.overdue ? " · 当时逾期" : ""} · 当前状态：{member.current_status || "事务已删除"}</span></li>)}</ul>{selected.kind === "recovery_summary" && <button onClick={() => onOpenSummary(selected.id)}>查看摘要事务</button>}<ul>{selected.deliveries.map((delivery) => <li key={delivery.claim_id}>{delivery.status === "accepted" ? "已交给浏览器" : delivery.status === "failed" ? `投递失败：${delivery.error}` : "结果未确认"}</li>)}</ul><button onClick={() => setSelected(null)}>关闭</button></article>}
  </section>;
}
