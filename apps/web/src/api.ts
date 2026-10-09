export interface Session {
  user: { id: string; username: string; role: string };
  csrf_token: string;
}
export interface Connection {
  id: string;
  kind: "outlook_plus" | "cloudflare_temp_email" | "cymail";
  name: string;
  base_url: string;
  status: string;
  last_error: string | null;
  last_checked_at: string | null;
  config_summary?: Record<string, string>;
}
export interface Mailbox {
  id: string;
  connection_id: string;
  address: string;
  display_name: string | null;
  sync_state: string;
  last_synced_at: string | null;
}
export interface SyncRun {
  id: string;
  mailbox_id: string;
  status: string;
  error_code: string | null;
  new_message_count: number;
}
export interface Message {
  id: string;
  mailbox_id: string;
  sender: string;
  recipient: string;
  subject: string;
  snippet: string;
  received_at: string;
  verification_code: string | null;
  body_text?: string;
}
export const providerLabels: Record<Connection["kind"], string> = {
  outlook_plus: "OutlookEmailPlus",
  cloudflare_temp_email: "Cloudflare 临时邮箱",
  cymail: "CYMail",
};
const errors: Record<string, string> = {
  UPSTREAM_UNAVAILABLE: "上游服务暂不可用，请稍后重试。",
  MAILBOX_ALREADY_REGISTERED: "该邮箱已登记，请在列表中同步。",
  UPSTREAM_TIMEOUT: "上游响应超时，请重试。",
  UPSTREAM_AUTH: "上游鉴权失败，请检查服务端凭据。",
  UPSTREAM_CONTRACT: "上游响应格式异常，请检查服务版本。",
  UPSTREAM_ORIGIN_NOT_ALLOWED: "服务地址不在部署白名单中，请检查配置。",
  UPSTREAM_ORIGIN_INVALID: "服务地址格式不正确，请填写完整服务源地址。",
  UPSTREAM_HTTPS_REQUIRED: "服务地址需要使用 HTTPS。",
  LOGIN_RATE_LIMITED: "登录尝试过于频繁，请稍后重试。",
  MAILBOX_NOT_READABLE: "上游邮箱不可读，请检查授权。",
};
export function errorText(code: string | null | undefined) {
  return code ? (errors[code] ?? "操作失败，请检查配置后重试。") : "";
}
export function displayTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString("zh-CN") : "尚无记录";
}
export function statusText(value: string) {
  return (
    (
      {
        active: "可用",
        idle: "待同步",
        running: "同步中",
        success: "同步成功",
        partial: "部分成功",
        error: "同步失败",
        failed: "同步失败",
      } as Record<string, string>
    )[value] ?? "状态待检查"
  );
}
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  csrfToken?: string,
): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//"))
    throw new Error("请求路径不正确。");
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (
    csrfToken &&
    !["GET", "HEAD"].includes((init.method ?? "GET").toUpperCase())
  )
    headers.set("X-CSRF-Token", csrfToken);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...init,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new Error("网络连接失败，请重试。");
  }
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new Event("mails:unauthorized"));
      throw new Error(
        init.method === "POST" && path === "/session"
          ? "用户名或密码错误，请重新输入。"
          : "会话已过期，请重新登录。",
      );
    }
    const data = await response.json().catch(() => null);
    const code = typeof data?.detail === "string" ? data.detail : null;
    throw new Error(
      code && errors[code]
        ? errors[code]
        : response.status === 403
          ? "请求校验失败，请刷新页面后重试。"
          : response.status === 422
            ? "输入格式不正确，请检查表单。"
            : "操作失败，请检查配置后重试。",
    );
  }
  return response.status === 204 ? (null as T) : response.json();
}
