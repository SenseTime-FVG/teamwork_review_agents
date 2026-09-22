import type { ChangeRequestRecord } from "./types.ts";

export function manualTriggerEvent(record: ChangeRequestRecord) {
  // 显式空候选必须尊重后端判断；只有旧接口缺字段时才兼容平台活动。
  if (record.manual_event !== undefined) return record.manual_event;
  if (record.latest_event_error || !record.latest_event) return null;
  return { ...record.latest_event, source: "platform" as const };
}

export function activitySourceLabel(source: "platform" | "system") {
  return source === "platform" ? "平台活动" : "系统检测";
}

export function platformActivityStatus(record: ChangeRequestRecord) {
  // 读取失败与无事件分开，不能把旧缓存展示成刚刚验证成功的结果。
  if (record.latest_event_error) return "平台活动读取失败";
  if (record.latest_event_supported === false) return "该平台尚未适配活动读取";
  return record.latest_event_checked ? "暂无可识别平台事件" : "等待扫描获取";
}
