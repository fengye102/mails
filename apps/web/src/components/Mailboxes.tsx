import { useEffect, useState, type FormEvent } from "react";
import {
  apiFetch,
  displayTime,
  errorText,
  statusText,
  type Connection,
  type Mailbox,
  type SyncRun,
} from "../api";
export function Mailboxes({ csrfToken }: { csrfToken: string }) {
  const [items, setItems] = useState<Mailbox[]>([]),
    [connections, setConnections] = useState<Connection[]>([]),
    [runs, setRuns] = useState<SyncRun[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true),
    [saving, setSaving] = useState(false),
    [syncing, setSyncing] = useState<string[]>([]);
  async function load() {
    setLoading(true);
    setError("");
    try {
      const [boxes, links, history] = await Promise.all([
        apiFetch<Mailbox[]>("/mailboxes"),
        apiFetch<Connection[]>("/connections"),
        apiFetch<SyncRun[]>("/sync-runs"),
      ]);
      setItems(boxes);
      setConnections(links);
      setRuns(history);
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
    setSaving(true);
    setError("");
    try {
      const item = await apiFetch<Mailbox>(
        `/connections/${encodeURIComponent(String(data.get("connection")))}/mailboxes`,
        {
          method: "POST",
          body: JSON.stringify({
            address: data.get("address"),
            display_name: data.get("display_name") || null,
          }),
        },
        csrfToken,
      );
      setItems((current) => [...current, item]);
      form.reset();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  async function sync(id: string) {
    setSyncing((current) => [...current, id]);
    setError("");
    try {
      const run = await apiFetch<SyncRun>(
        `/mailboxes/${encodeURIComponent(id)}/sync`,
        { method: "POST" },
        csrfToken,
      );
      setRuns((current) => [
        run,
        ...current.filter((item) => item.id !== run.id),
      ]);
      setItems(await apiFetch<Mailbox[]>("/mailboxes"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSyncing((current) => current.filter((item) => item !== id));
    }
  }
  return (
    <section>
      <h1>邮箱</h1>
      <p>登记已在上游授权的邮箱地址，再手动同步最近邮件。</p>
      <form className="form-grid" onSubmit={submit}>
        <label>
          来源连接
          <select name="connection" required defaultValue="">
            <option value="">选择连接</option>
            {connections.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          邮箱地址
          <input name="address" type="email" required maxLength={320} />
        </label>
        <label>
          显示名称
          <input name="display_name" maxLength={256} />
        </label>
        <button disabled={saving || !connections.length}>
          {saving ? "登记中…" : "登记邮箱"}
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      <button disabled={loading} onClick={() => void load()}>
        刷新邮箱状态
      </button>
      {loading ? (
        <p role="status">正在读取邮箱…</p>
      ) : items.length === 0 ? (
        <p>尚无邮箱。先添加来源连接，再登记邮箱地址。</p>
      ) : (
        <ul className="records">
          {items.map((item) => {
            const run = runs.find((run) => run.mailbox_id === item.id);
            return (
              <li key={item.id}>
                <h2>{item.address}</h2>
                {item.display_name && <p>{item.display_name}</p>}
                <p>{statusText(run?.status ?? item.sync_state)}</p>
                <p>最近同步：{displayTime(item.last_synced_at)}</p>
                {run && <p>新增邮件：{run.new_message_count}</p>}
                {run?.error_code && (
                  <p role="alert">{errorText(run.error_code)}</p>
                )}
                <button
                  disabled={syncing.includes(item.id)}
                  aria-label={`同步 ${item.address}`}
                  onClick={() => void sync(item.id)}
                >
                  {syncing.includes(item.id) ? "同步中…" : "手动同步"}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
