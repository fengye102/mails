import { useEffect, useState, type FormEvent } from "react";
import { displayTime } from "../../api";

/** 管理适配器返回的统一视图（对应后端 SourceItem.payload()）。 */
export interface SourceItem {
  provider_key: string;
  address: string;
  status: string;
  created_at: string | null;
  detail: Record<string, unknown>;
}

export interface SourceMail {
  provider_key: string;
  address: string | null;
  source: string | null;
  message_id: string | null;
  created_at: string | null;
  is_unread: boolean;
}

/** 查看地址口令的响应；value 是上游凭据，只在内存里展示。 */
export interface SourcePassword {
  provider_key: string;
  sensitive: boolean;
  field: string;
  value: string;
}

const SOURCE_ERRORS: Record<string, string> = {
  SOURCE_AUTH: "上游鉴权失败，请检查该连接的管理员口令。",
  SOURCE_TIMEOUT: "上游响应超时，请重试。",
  SOURCE_UNAVAILABLE: "上游服务暂不可用，请稍后重试。",
  SOURCE_CONTRACT: "上游响应格式异常，请检查服务版本。",
  SOURCE_NOT_FOUND: "上游资源不存在或已被删除。",
  SOURCE_CONFLICT: "上游状态冲突，请先刷新列表。",
  SOURCE_REJECTED: "上游拒绝了该操作，请检查输入。",
  CONNECTION_NOT_FOUND: "来源连接不存在，请刷新来源连接。",
  UNSUPPORTED_PROVIDER_KIND: "该连接不是 Cloudflare 临时邮箱来源。",
  CONNECTION_SECRET_INVALID: "连接密钥无法解密，请重新登记连接。",
  PASSWORD_INVALID: "口令长度不合法。",
};

export function sourceErrorMessage(code: string | null | undefined) {
  return code ? (SOURCE_ERRORS[code] ?? "操作失败，请检查配置后重试。") : "";
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  csrfToken?: string,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  const method = (init.method ?? "GET").toUpperCase();
  if (csrfToken && !["GET", "HEAD"].includes(method))
    headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new Event("mails:unauthorized"));
      throw new Error("会话已过期，请重新登录。");
    }
    const data = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    const code = typeof data?.detail === "string" ? data.detail : null;
    throw new Error(
      sourceErrorMessage(code) || "操作失败，请检查配置后重试。",
    );
  }
  return response.status === 204 ? (null as T) : response.json();
}

export function CloudflareTempEmailPanel({
  connectionId,
  csrfToken,
}: {
  connectionId: string;
  csrfToken: string;
}) {
  const base = `/sources/cloudflare_temp_email/${encodeURIComponent(connectionId)}`;
  const [items, setItems] = useState<SourceItem[]>([]);
  const [mails, setMails] = useState<Record<string, SourceMail[]>>({});
  const [password, setPassword] = useState<SourcePassword | null>(null);
  const [resetFor, setResetFor] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    setError("");
    try {
      setItems(await request<SourceItem[]>(`${base}/items`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, [connectionId]);

  async function guard(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const domain = String(data.get("domain") ?? "").trim();
    await guard(async () => {
      const item = await request<SourceItem>(
        `${base}/items`,
        {
          method: "POST",
          body: JSON.stringify({
            name: data.get("name"),
            domain: domain || null,
            enable_prefix: data.get("enable_prefix") === "on",
          }),
        },
        csrfToken,
      );
      setItems((current) => [item, ...current]);
      form.reset();
    });
  }

  async function remove(key: string) {
    await guard(async () => {
      await request<void>(`${base}/items/${encodeURIComponent(key)}`, { method: "DELETE" }, csrfToken);
      setItems((current) => current.filter((item) => item.provider_key !== key));
      setPassword((current) => (current?.provider_key === key ? null : current));
    });
  }

  async function clearInbox(key: string) {
    await guard(async () => {
      await request<void>(
        `${base}/items/${encodeURIComponent(key)}/clear_inbox`,
        { method: "POST" },
        csrfToken,
      );
      setMails((current) => ({ ...current, [key]: [] }));
    });
  }

  async function showPassword(key: string) {
    await guard(async () => {
      setPassword(
        await request<SourcePassword>(`${base}/items/${encodeURIComponent(key)}/password`),
      );
    });
  }

  async function resetPassword(event: FormEvent<HTMLFormElement>, key: string) {
    event.preventDefault();
    const form = event.currentTarget;
    const value = String(new FormData(form).get("password") ?? "");
    await guard(async () => {
      await request<void>(
        `${base}/items/${encodeURIComponent(key)}/password`,
        { method: "POST", body: JSON.stringify({ password: value }) },
        csrfToken,
      );
      setResetFor("");
      form.reset();
    });
  }

  async function loadMails(key: string, address: string) {
    await guard(async () => {
      const result = await request<{ count: number; items: SourceMail[] }>(
        `${base}/mails?address=${encodeURIComponent(address)}&limit=50&offset=0`,
      );
      setMails((current) => ({ ...current, [key]: result.items }));
    });
  }

  async function deleteMail(key: string, mailKey: string) {
    await guard(async () => {
      await request<void>(
        `${base}/mails/${encodeURIComponent(mailKey)}`,
        { method: "DELETE" },
        csrfToken,
      );
      setMails((current) => ({
        ...current,
        [key]: (current[key] ?? []).filter((mail) => mail.provider_key !== mailKey),
      }));
    });
  }

  return (
    <section>
      <h1>Cloudflare 临时邮箱</h1>
      <p>直接在上游 Worker 上创建、清理邮箱并查看口令；这些动作不写入本地数据库。</p>
      <form className="form-grid" onSubmit={create}>
        <label>
          地址名称
          <input name="name" required maxLength={64} placeholder="tmp" />
        </label>
        <label>
          域名
          <input name="domain" maxLength={253} placeholder="可留空" />
        </label>
        <label>
          随机前缀
          <input name="enable_prefix" type="checkbox" />
        </label>
        <button disabled={busy}>创建地址</button>
      </form>
      {error && <p role="alert">{error}</p>}
      <button disabled={loading || busy} onClick={() => void load()}>
        刷新地址列表
      </button>
      {password && (
        <p role="status" className="sensitive">
          地址 {password.provider_key} 的{password.field === "jwt" ? "登录凭据" : password.field}：
          <code>{password.value}</code>（敏感字段，仅本次显示）
        </p>
      )}
      {loading ? (
        <p role="status">正在读取地址列表…</p>
      ) : items.length === 0 ? (
        <p>上游暂无邮箱地址。</p>
      ) : (
        <ul className="records">
          {items.map((item) => (
            <li key={item.provider_key}>
              <h2>{item.address}</h2>
              <p>状态：{item.status}</p>
              <p>创建时间：{displayTime(item.created_at)}</p>
              <button
                aria-label={`查看口令 ${item.address}`}
                onClick={() => void showPassword(item.provider_key)}
              >
                查看口令
              </button>
              <button
                aria-label={`重置口令 ${item.address}`}
                onClick={() => setResetFor(item.provider_key)}
              >
                重置口令
              </button>
              <button
                aria-label={`清空收件箱 ${item.address}`}
                onClick={() => void clearInbox(item.provider_key)}
              >
                清空收件箱
              </button>
              <button
                aria-label={`查看邮件 ${item.address}`}
                onClick={() => void loadMails(item.provider_key, item.address)}
              >
                查看邮件
              </button>
              <button
                aria-label={`删除 ${item.address}`}
                onClick={() => void remove(item.provider_key)}
              >
                删除
              </button>
              {resetFor === item.provider_key && (
                <form onSubmit={(event) => void resetPassword(event, item.provider_key)}>
                  <label>
                    新口令
                    <input name="password" type="password" required maxLength={256} />
                  </label>
                  <button disabled={busy}>确认重置</button>
                </form>
              )}
              {(mails[item.provider_key] ?? []).length > 0 && (
                <ul>
                  {(mails[item.provider_key] ?? []).map((mail) => (
                    <li key={mail.provider_key}>
                      <span>{mail.source ?? "未知来源"}</span>
                      <span> {displayTime(mail.created_at)}</span>
                      <button
                        aria-label={`删除邮件 ${mail.provider_key}`}
                        onClick={() => void deleteMail(item.provider_key, mail.provider_key)}
                      >
                        删除邮件
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
