export interface PickupSession {
  pickup_id: string;
  allocation_id: string;
  session_token: string;
  expires_at: string;
}

export interface DeliveryMessage {
  id: string;
  sender: string;
  subject: string;
  received_at: string;
  body_text: string;
  verification_code: string | null;
}

export interface DeliveryMailbox {
  address: string;
  messages: DeliveryMessage[];
}

const errors: Record<string, string> = {
  DELIVERY_PICKUP_INVALID: "取件码无效、已使用或已过期。",
  DELIVERY_PICKUP_RETRY_LATER: "尝试次数过多，请稍后再试。",
  DELIVERY_PICKUP_SESSION_INVALID: "取件会话已失效，请重新输入取件码。",
  DELIVERY_PICKUP_SCOPE_INVALID: "该取件会话无权读取此邮箱。",
  DELIVERY_MAILBOX_NOT_FOUND: "邮箱不存在。",
  DELIVERY_ALLOCATION_EXPIRED: "该邮箱的交付有效期已过。",
  DELIVERY_OPERATOR_REQUIRED: "此接口仅限运营方调用。",
};

export function errorText(code: string | null | undefined) {
  return code ? (errors[code] ?? "操作失败，请稍后重试。") : "操作失败，请稍后重试。";
}

export function displayTime(value: string) {
  return new Date(value).toLocaleString("zh-CN");
}

export function formatRemaining(seconds: number) {
  if (seconds <= 0) return "已过期";
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) return `剩余约 ${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  if (minutes >= 1) return `剩余约 ${minutes} 分钟`;
  return `剩余 ${seconds} 秒`;
}

/** 后端固定返回 {"detail": {"code": "..."}}；校验错误是数组，这里统一降级为通用文案。 */
function detailCode(payload: unknown): string | null {
  const detail = (payload as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === "string") return code;
  }
  return null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("请求路径不正确。");
  }
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`/delivery-api/v1${path}`, {
      ...init,
      headers,
      cache: "no-store",
    });
  } catch {
    throw new Error("网络连接失败，请重试。");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(errorText(detailCode(payload)));
  }
  return response.status === 204 ? (null as T) : response.json();
}

export async function verifyPickup(pickupCode: string): Promise<PickupSession> {
  return request<PickupSession>("/pickups/verify", {
    method: "POST",
    body: JSON.stringify({ pickup_code: pickupCode }),
  });
}

export async function readMailbox(
  allocationId: string,
  sessionToken: string,
): Promise<DeliveryMailbox> {
  return request<DeliveryMailbox>(
    `/pickups/${encodeURIComponent(allocationId)}/mailbox`,
    { headers: { Authorization: `Bearer ${sessionToken}` } },
  );
}
