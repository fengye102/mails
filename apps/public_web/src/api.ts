export interface PublicMailbox {
  id: string;
  address: string;
  status: "active" | "expired" | "upstream_error" | string;
  expires_at: string;
  remaining_seconds: number;
}

export interface PublicMessage {
  id: string;
  sender: string;
  recipient?: string;
  subject: string;
  snippet: string;
  body_text?: string;
  received_at: string;
  verification_code: string | null;
}

const errors: Record<string, string> = {
  PUBLIC_MAILBOX_EXPIRED: "这个临时邮箱已过期。",
  PUBLIC_MAILBOX_NOT_FOUND: "临时邮箱不存在或已失效。",
  PUBLIC_MAIL_CAPABILITY_REQUIRED: "请使用创建邮箱的浏览器会话访问。",
  PUBLIC_MAIL_SESSION_LIMITED: "当前会话的临时邮箱数量已达上限。",
  PUBLIC_MAIL_RATE_LIMITED: "请求过于频繁，请稍后重试。",
  PUBLIC_MAIL_CREATE_RATE_LIMITED: "创建次数已达上限，请稍后再试。",
  PUBLIC_MAIL_LIST_RATE_LIMITED: "刷新过于频繁，请稍后重试。",
  PUBLIC_MAIL_DETAIL_RATE_LIMITED: "查看过于频繁，请稍后重试。",
  PUBLIC_MAIL_UPSTREAM_AUTH: "临时邮箱服务暂不可用，请稍后重试。",
  PUBLIC_MAIL_UPSTREAM_TIMEOUT: "临时邮箱服务响应超时，请稍后重试。",
  PUBLIC_MAIL_UPSTREAM_CONTRACT: "临时邮箱服务返回了无效数据。",
  PUBLIC_MAIL_UPSTREAM_UNAVAILABLE: "临时邮箱服务暂不可用，请稍后重试。",
  PUBLIC_MAIL_UPSTREAM_ERROR: "临时邮箱服务暂不可用，请稍后重试。",
};

export function errorText(code: string | null | undefined) {
  return code ? (errors[code] ?? "操作失败，请稍后重试。") : "操作失败，请稍后重试。";
}

export function displayTime(value: string) {
  return new Date(value).toLocaleString("zh-CN");
}

export function formatRemaining(seconds: number) {
  if (seconds <= 0) return "已过期";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `剩余约 ${hours} 小时`;
  if (minutes) return `剩余约 ${minutes} 分钟`;
  return `剩余 ${seconds} 秒`;
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("请求路径不正确。");
  }
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/public-api/v1${path}`, {
      ...init,
      headers,
      credentials: "include",
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error("网络连接失败，请重试。");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const code = typeof data?.detail === "string" ? data.detail : null;
    if (response.status === 410 || code === "PUBLIC_MAILBOX_EXPIRED") {
      throw new Error(errors.PUBLIC_MAILBOX_EXPIRED);
    }
    throw new Error(errorText(code));
  }
  return response.status === 204 ? (null as T) : response.json();
}
