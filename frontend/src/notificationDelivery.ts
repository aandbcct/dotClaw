import type { NotificationItem } from "./types";

export interface NotificationDeliveryPort {
  permission(): "unsupported" | "default" | "denied" | "granted";
  requestPermission(): Promise<"default" | "denied" | "granted">;
  deliver(item: NotificationItem, onClick: () => void): Promise<void>;
}

export class BrowserNotificationDelivery implements NotificationDeliveryPort {
  permission() { return typeof window === "undefined" || !("Notification" in window) ? "unsupported" : Notification.permission; }
  async requestPermission() { return this.permission() === "unsupported" ? "denied" : await Notification.requestPermission(); }
  async deliver(item: NotificationItem, onClick: () => void) {
    const notification = new Notification(item.title, { body: item.body, tag: `dotclaw-${item.id}` });
    notification.onclick = () => { window.focus(); onClick(); notification.close(); };
  }
}
