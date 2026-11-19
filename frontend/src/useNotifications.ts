import { useCallback, useEffect, useRef, useState } from "react";
import { claimNotificationDelivery, getNotificationChanges, listNotifications, reportNotificationDelivery } from "./api";
import type { NotificationDeliveryPort } from "./notificationDelivery";
import type { NotificationItem } from "./types";

export function useNotifications(delivery: NotificationDeliveryPort, onNavigate: (item: NotificationItem) => void) {
  const [unreadCount, setUnreadCount] = useState(0);
  const [connectionError, setConnectionError] = useState("");
  const [permission, setPermission] = useState(delivery.permission());
  const cursor = useRef(0);
  const started = useRef(false);
  const navigating = useRef(onNavigate);
  const busy = useRef(false);
  navigating.current = onNavigate;
  const refresh = useCallback(async () => { const page = await listNotifications({ limit: 1 }); setUnreadCount(page.unread_count); return page; }, []);
  const enableSystemNotifications = useCallback(async () => { const value = await delivery.requestPermission(); setPermission(value); if (value === "granted") { const page = await refresh(); cursor.current = page.watermark; started.current = true; } }, [delivery, refresh]);
  useEffect(() => {
    let disposed = false;
    let timer = 0;
    let previousPoll = Date.now();
    let wasDisconnected = false;
    const controller = new AbortController();
    async function deliverNew(item: NotificationItem) {
      if (delivery.permission() !== "granted") return;
      const claim = await claimNotificationDelivery(item.id);
      if (!claim) return;
      try { await delivery.deliver(item, () => navigating.current(item)); }
      catch (error) { await reportNotificationDelivery(item.id, claim.claim_id, "failed", error instanceof Error ? error.message : "浏览器通知失败"); return; }
      await reportNotificationDelivery(item.id, claim.claim_id, "accepted", null);
    }
    async function poll() {
      if (disposed || busy.current) return;
      busy.current = true;
      try {
        const now = Date.now();
        if (started.current && wasDisconnected) { const page = await listNotifications({ limit: 1 }, controller.signal); cursor.current = page.watermark; setUnreadCount(page.unread_count); wasDisconnected = false; previousPoll = now; setConnectionError(""); return; }
        if (started.current && now - previousPoll > 30000) { const page = await listNotifications({ limit: 1 }, controller.signal); cursor.current = page.watermark; setUnreadCount(page.unread_count); previousPoll = now; setConnectionError(""); return; }
        if (!started.current) { const page = await listNotifications({ limit: 1 }, controller.signal); cursor.current = page.watermark; setUnreadCount(page.unread_count); started.current = true; previousPoll = now; return; }
        let more = true;
        while (more && !disposed) {
          const page = await getNotificationChanges(cursor.current, controller.signal);
          if (page.items.length) {
            for (const item of page.items) { if (item.id > cursor.current) await deliverNew(item); }
          }
          cursor.current = page.next_after_id; setUnreadCount(page.unread_count); more = page.has_more;
        }
        previousPoll = now; setConnectionError("");
      } catch (error) { if (!disposed && !controller.signal.aborted) { wasDisconnected = true; setConnectionError(error instanceof Error ? error.message : "通知连接中断"); } }
      finally { busy.current = false; }
    }
    void poll(); timer = window.setInterval(() => void poll(), 5000);
    return () => { disposed = true; controller.abort(); window.clearInterval(timer); };
  }, [delivery]);
  return { unreadCount, connectionError, permission, enableSystemNotifications, refresh };
}
