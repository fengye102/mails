import { useEffect, useState, type FormEvent } from "react";
import { apiFetch, displayTime } from "../../api";

export interface SourceItem {
  provider_key: string;
  address: string;
  status: string;
  created_at: string | null;
  detail: { id?: number } & Record<string, unknown>;
}

/**
 * OutlookEmailPlus（OEP 注册邮箱池）来源管理面板。
 *
 * 对接 `/api/v1/sources/outlook_plus/{connection_id}`：列表、导入、删除、刷新令牌、导出清单。
 * 账号串含明文口令，只在提交时发送，成功后立即清空输入框，任何错误信息都不回显口令。
 */
export function OutlookPlusPanel({
  connectionId,
  csrfToken,
}: {
  connectionId: string;
  csrfToken: string;
}) {
  const base = `/sources/outlook_plus/${encodeURIComponent(connectionId)}`;
  const [items, setItems] = useState<SourceItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    setError("");
    try {
      setItems(await apiFetch<SourceItem[]>(`${base}/items`));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, [connectionId]); // connectionId 变化时重新加载

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const accountString = String(data.get("account_string") ?? "");
    const groupId = Number(data.get("group_id") ?? 1);
    (form.elements.namedItem("account_string") as HTMLInputElement).value = "";
    setBusy(true);
    setError("");
    try {
      const item = await apiFetch<SourceItem>(
        `${base}/items`,
        {
          method: "POST",
          body: JSON.stringify({ account_string: accountString, group_id: groupId }),
        },
        csrfToken,
      );
      setItems((current) => [...current, item]);
      form.reset();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(providerKey: string) {
    setBusy(true);
    setError("");
    try {
      await apiFetch(
        `${base}/items/${encodeURIComponent(providerKey)}`,
        { method: "DELETE" },
        csrfToken,
      );
      setItems((current) => current.filter((item) => item.provider_key !== providerKey));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function refresh(accountId: number) {
    setBusy(true);
    setError("");
    try {
      await apiFetch(`${base}/accounts/${accountId}/refresh`, { method: "POST" }, csrfToken);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h1>OutlookEmailPlus 邮箱池</h1>
      <p>管理注册邮箱池账号；账号口令仅用于提交，不会回显。</p>
      <form className="form-grid" onSubmit={submit}>
        <label>
          账号串（邮箱----密码----client_id----refresh_token）
          <input name="account_string" required autoComplete="off" />
        </label>
        <label>
          分组 ID
          <input name="group_id" type="number" min={1} defaultValue={1} />
        </label>
        <button disabled={busy}>{busy ? "提交中…" : "导入账号"}</button>
      </form>
      <a href={`/api/v1${base}/export`} download>
        导出账号清单
      </a>
      {error && <p role="alert">{error}</p>}
      <button onClick={() => void load()} disabled={loading}>
        刷新列表
      </button>
      {loading ? (
        <p role="status">正在读取账号…</p>
      ) : items.length === 0 ? (
        <p>该连接下暂无账号。</p>
      ) : (
        <ul className="records">
          {items.map((item) => {
            const accountId = item.detail.id;
            return (
              <li key={item.provider_key}>
                <h2>{item.address}</h2>
                <p>
                  {item.status} / 创建：{displayTime(item.created_at)}
                </p>
                <button disabled={busy} onClick={() => void remove(item.provider_key)}>
                  删除
                </button>
                <button
                  disabled={busy || typeof accountId !== "number"}
                  onClick={() => {
                    if (typeof accountId === "number") void refresh(accountId);
                  }}
                >
                  刷新令牌
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
