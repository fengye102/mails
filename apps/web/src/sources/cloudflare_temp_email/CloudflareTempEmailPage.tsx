import { useEffect, useState } from "react";
import { apiFetch, providerLabels, type Connection } from "../../api";
import { CloudflareTempEmailPanel } from "./CloudflareTempEmailPanel";

/** 选择一条 Cloudflare 临时邮箱来源连接，再交给管理面板。 */
export function CloudflareTempEmailPage({ csrfToken }: { csrfToken: string }) {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    void apiFetch<Connection[]>("/connections")
      .then((items) => {
        if (!active) return;
        const sources = items.filter((item) => item.kind === "cloudflare_temp_email");
        setConnections(sources);
        setSelected((current) => current || sources[0]?.id || "");
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

  if (loading)
    return (
      <main>
        <p role="status">正在读取来源连接…</p>
      </main>
    );
  if (error)
    return (
      <main>
        <p role="alert">{error}</p>
      </main>
    );
  if (connections.length === 0)
    return (
      <section>
        <h1>Cloudflare 临时邮箱</h1>
        <p>尚无 Cloudflare 临时邮箱来源连接，请先在「来源连接」里登记。</p>
      </section>
    );
  return (
    <section>
      <label>
        来源连接
        <select value={selected} onChange={(event) => setSelected(event.target.value)}>
          {connections.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}（{providerLabels[item.kind]}）
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <CloudflareTempEmailPanel connectionId={selected} csrfToken={csrfToken} />
      )}
    </section>
  );
}
