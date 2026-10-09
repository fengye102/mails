import { useEffect, useState, type FormEvent } from "react";
import {
  apiFetch,
  displayTime,
  errorText,
  statusText,
  type Connection,
} from "../api";
export function Connections({ csrfToken }: { csrfToken: string }) {
  const [items, setItems] = useState<Connection[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false);
  async function load() {
    setLoading(true);
    setError("");
    try {
      setItems(await apiFetch<Connection[]>("/connections"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const body = JSON.stringify({
      kind: "outlook_plus",
      name: data.get("name"),
      base_url: data.get("base_url"),
      api_key: data.get("api_key"),
    });
    (form.elements.namedItem("api_key") as HTMLInputElement).value = "";
    setBusy(true);
    setError("");
    try {
      const item = await apiFetch<Connection>(
        "/connections",
        { method: "POST", body },
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
  return (
    <section>
      <h1>来源连接</h1>
      <p>接入已配置的 OutlookEmailPlus 服务。密钥仅用于提交，不会回显。</p>
      <form className="form-grid" onSubmit={submit}>
        <label>
          连接名称
          <input name="name" required maxLength={256} />
        </label>
        <label>
          服务地址
          <input
            name="base_url"
            type="url"
            placeholder="https://mail.example.com"
            required
            maxLength={2048}
          />
        </label>
        <label>
          API Key
          <input
            name="api_key"
            type="password"
            required
            maxLength={8192}
            autoComplete="off"
          />
        </label>
        <button disabled={busy}>{busy ? "验证中…" : "验证并添加连接"}</button>
      </form>
      {error && <p role="alert">{error}</p>}
      <button onClick={() => void load()} disabled={loading}>
        刷新连接
      </button>
      {loading ? (
        <p role="status">正在读取连接…</p>
      ) : items.length === 0 ? (
        <p>尚无连接，请先登记 OutlookEmailPlus 服务。</p>
      ) : (
        <ul className="records">
          {items.map((item) => (
            <li key={item.id}>
              <h2>{item.name}</h2>
              <p>{item.base_url}</p>
              <p>
                {statusText(item.status)} / 最近检查：
                {displayTime(item.last_checked_at)}
              </p>
              {item.last_error && (
                <p role="alert">{errorText(item.last_error)}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
