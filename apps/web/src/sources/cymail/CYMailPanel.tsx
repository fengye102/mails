import { useEffect, useState, type FormEvent } from "react";
import { apiFetch, displayTime, type Connection } from "../../api";

export interface SourceItem {
  provider_key: string;
  address: string;
  status: string;
  created_at: string | null;
  detail: Record<string, unknown>;
}

const statusLabels: Record<string, string> = {
  active: "可用",
  inactive: "已停用",
};

/**
 * CYMail（iCloud 隐藏邮箱）来源管理面板：列出/创建/停用/启用/删除 HME 别名。
 * 所有写操作携带 CSRF；密钥只在服务端使用，不进入浏览器。
 */
export function CYMailPanel({ csrfToken }: { csrfToken: string }) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [connectionId, setConnectionId] = useState("");
  const [items, setItems] = useState<SourceItem[]>([]);
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void apiFetch<Connection[]>("/connections")
      .then((all) => {
        if (!active) return;
        const cymail = all.filter((item) => item.kind === "cymail");
        setConnections(cymail);
        setConnectionId((current) => current || (cymail[0]?.id ?? ""));
      })
      .catch((e) => {
        if (active) setError((e as Error).message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!connectionId) {
      setItems([]);
      return;
    }
    let active = true;
    setLoading(true);
    setError("");
    void apiFetch<SourceItem[]>(
      `/sources/cymail/aliases?connection_id=${encodeURIComponent(connectionId)}`,
    )
      .then((value) => {
        if (active) setItems(value);
      })
      .catch((e) => {
        if (active) setError((e as Error).message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [connectionId, reloadKey]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const label = String(new FormData(form).get("label") ?? "");
    setBusy(true);
    setError("");
    try {
      const item = await apiFetch<SourceItem>(
        `/sources/cymail/aliases?connection_id=${encodeURIComponent(connectionId)}`,
        { method: "POST", body: JSON.stringify({ label }) },
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

  async function act(providerKey: string, action: "deactivate" | "reactivate") {
    setBusy(true);
    setError("");
    try {
      await apiFetch(
        `/sources/cymail/aliases/${encodeURIComponent(providerKey)}/${action}` +
          `?connection_id=${encodeURIComponent(connectionId)}`,
        { method: "POST" },
        csrfToken,
      );
      setItems((current) =>
        current.map((item) =>
          item.provider_key === providerKey
            ? { ...item, status: action === "deactivate" ? "inactive" : "active" }
            : item,
        ),
      );
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
        `/sources/cymail/aliases/${encodeURIComponent(providerKey)}` +
          `?connection_id=${encodeURIComponent(connectionId)}`,
        { method: "DELETE" },
        csrfToken,
      );
      setItems((current) =>
        current.filter((item) => item.provider_key !== providerKey),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h1>CYMail 隐藏邮箱</h1>
      <p>管理 iCloud 隐藏邮箱（HME）别名：创建、停用、启用与删除。</p>
      {error && <p role="alert">{error}</p>}
      {loading && connections.length === 0 ? (
        <p role="status">正在读取 CYMail 连接…</p>
      ) : connections.length === 0 ? (
        <p>尚无 CYMail 连接，请先在「来源连接」登记。</p>
      ) : (
        <>
          <label>
            连接
            <select
              value={connectionId}
              onChange={(event) => setConnectionId(event.target.value)}
            >
              {connections.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <form className="form-grid" onSubmit={create}>
            <label>
              标签
              <input name="label" maxLength={256} autoComplete="off" />
            </label>
            <button disabled={busy || !connectionId}>创建隐藏邮箱</button>
          </form>
          <button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>
            刷新列表
          </button>
          {loading ? (
            <p role="status">正在读取别名…</p>
          ) : items.length === 0 ? (
            <p>该连接下暂无隐藏邮箱别名。</p>
          ) : (
            <ul className="records">
              {items.map((item) => (
                <li key={item.provider_key}>
                  <h2>{item.address}</h2>
                  <p>{statusLabels[item.status] ?? item.status}</p>
                  {typeof item.detail?.label === "string" && item.detail.label && (
                    <p>标签：{item.detail.label}</p>
                  )}
                  <p>创建时间：{displayTime(item.created_at)}</p>
                  <button disabled={busy} onClick={() => void act(item.provider_key, "deactivate")}>
                    停用
                  </button>
                  <button disabled={busy} onClick={() => void act(item.provider_key, "reactivate")}>
                    启用
                  </button>
                  <button disabled={busy} onClick={() => void remove(item.provider_key)}>
                    删除
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
