import type { Reminder, ReminderRule } from "./types";

const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export function reminderRuleSummary(rule: ReminderRule, timezone: string): string {
  if (rule.kind === "at") return `指定时刻 · ${new Intl.DateTimeFormat("zh-CN", { timeZone: timezone, dateStyle: "medium", timeStyle: "short" }).format(new Date(rule.at))}`;
  if (rule.kind === "before_deadline") {
    const unit = rule.offset_minutes % 1440 === 0 ? "天" : rule.offset_minutes % 60 === 0 ? "小时" : "分钟";
    const value = unit === "天" ? rule.offset_minutes / 1440 : unit === "小时" ? rule.offset_minutes / 60 : rule.offset_minutes;
    return `截止前${value}${unit}`;
  }
  if (rule.kind === "before_date") return rule.days_before === 0 ? `截止当天 ${rule.local_time}` : `截止日期前${rule.days_before}天 ${rule.local_time}`;
  if (rule.kind === "daily") return `每天 ${rule.local_time}`;
  if (rule.kind === "weekly") return `每${rule.weekdays.map((day) => weekdays[day]).join("、")} ${rule.local_time}`;
  if (rule.kind === "monthly") return `每月${rule.day}日 ${rule.local_time}${rule.day > 28 ? "（当月无此日期时跳过）" : ""}`;
  return `每年${rule.month}月${rule.day}日 ${rule.local_time}${rule.month === 2 && rule.day === 29 ? "（平年跳过）" : ""}`;
}

export function reminderStatusLabel(status: string): string {
  return ({ active: "进行中", paused: "已暂停", cancelled: "已取消", fired: "已触发", missed: "已错过", completed: "已完成" } as Record<string, string>)[status] || status;
}

export function reminderReasonLabel(reason: string | null): string | null {
  if (!reason) return null;
  return ({ deadline_moved_past: "截止改期后提醒时刻已过去，已暂停", deadline_removed_or_changed_kind: "截止已删除或类型变化，提醒已取消", invalid_local_time: "该当地时刻不存在，已暂停" } as Record<string, string>)[reason] || reason;
}

export function nextReminderLabel(reminder: Reminder): string | null {
  if (!reminder.next_fire_at) return null;
  return new Intl.DateTimeFormat("zh-CN", { timeZone: reminder.timezone, dateStyle: "medium", timeStyle: "short" }).format(new Date(reminder.next_fire_at));
}
