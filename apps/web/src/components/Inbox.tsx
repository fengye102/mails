import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  apiFetch,
  displayTime,
  identityLabels,
  providerLabels,
  type Connection,
  type LiveMessage,
  type Mailbox,
  type Message,
} from "../api";
type Mode = "archive" | "live";
const addressSource = (kind: Connection["kind"], connectionId: string) =>
  kind === "cloudflare_temp_email"
    ? `/sources/cloudflare_temp_email/${connectionId}/items`
    : kind === "outlook_plus"
      ? `/sources/outlook_plus/${connectionId}/items`
      : "";
const asMessage = (item: LiveMessage): Message => ({
  id: item.provider_key,
  mailbox_id: "",
  recipient: "",
  source_kind: item.source_kind,
  sender: item.sender,
  subject: item.subject,
  snippet: item.snippet,
  received_at: item.received_at,
  verification_code: item.verification_code,
});
export function Inbox() {
  const [mode, setMode] = useState<Mode>("archive"),
    [boxes, setBoxes] = useState<Mailbox[]>([]),
    [items, setItems] = useState<Message[]>([]),
    [total, setTotal] = useState(0),
    [detail, setDetail] = useState<Message | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [loading, setLoading] = useState(true),
    [reading, setReading] = useState(false),
    [query, setQuery] = useState(""),
    [offset, setOffset] = useState(0),
    [connections, setConnections] = useState<Connection[]>([]),
    [liveConnection, setLiveConnection] = useState(""),
    [liveAddress, setLiveAddress] = useState(""),
    [liveQuery, setLiveQuery] = useState(""),
    [liveAddresses, setLiveAddresses] = useState<string[]>([]);
  const listRequest = useRef(0),
    detailRequest = useRef(0),
    heading = useRef<HTMLHeadingElement>(null);
  async function load(search: string, page: number) {
    const request = ++listRequest.current;
    setLoading(true);
    setError("");
    setDetail(null);
    detailRequest.current++;
    setReading(false);
    try {
      const result = await apiFetch<{ items: Message[]; total: number }>(
        `/messages?${search}&limit=50&offset=${page}`,
      );
      if (request === listRequest.current) {
        setItems(result.items);
        setTotal(result.total);
      }
    } catch (e) {
      if (request === listRequest.current) {
        setItems([]);
        setError((e as Error).message);
      }
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }
  async function loadLive(
    connectionId: string,
    address: string,
    search: string,
    page: number,
  ) {
    const request = ++listRequest.current;
    setLoading(true);
    setError("");
    setDetail(null);
    detailRequest.current++;
    setReading(false);
    const params = new URLSearchParams({ address, limit: "50", offset: String(page) });
    if (search) params.set("query", search);
    try {
      const result = await apiFetch<{ items: LiveMessage[]; total: number }>(
        `/connections/${connectionId}/live-mail?${params.toString()}`,
      );
      if (request === listRequest.current) {
        setItems(result.items.map(asMessage));
        setTotal(result.total);
      }
    } catch (e) {
      if (request === listRequest.current) {
        setItems([]);
        setError((e as Error).message);
      }
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }
  useEffect(() => {
    void apiFetch<Mailbox[]>("/mailboxes")
      .then(setBoxes)
      .catch((e) => setError(e.message));
    void load("", 0);
    return () => {
      listRequest.current++;
      detailRequest.current++;
    };
  }, []);
  useEffect(() => {
    if (detail) heading.current?.focus();
  }, [detail]);
  useEffect(() => {
    if (mode !== "live" || liveConnection || connections.length === 0) return;
    setLiveConnection(connections[0].id);
  }, [connections, mode, liveConnection]);
  useEffect(() => {
    const connection = connections.find((item) => item.id === liveConnection);
    const url = connection ? addressSource(connection.kind, connection.id) : "";
    if (!url) {
      setLiveAddresses([]);
      return;
    }
    let active = true;
    void apiFetch<{ address: string }[]>(url)
      .then((list) => {
        if (!active) return;
        setLiveAddresses(
          Array.isArray(list) ? list.map((item) => item.address).filter(Boolean) : [],
        );
      })
      .catch(() => {
        if (active) setLiveAddresses([]);
      });
    return () => {
      active = false;
    };
  }, [connections, liveConnection]);
  function switchMode(next: Mode) {
    if (next === mode) return;
    setMode(next);
    setError("");
    setNotice("");
    setDetail(null);
    detailRequest.current++;
    setItems([]);
    setTotal(0);
    setOffset(0);
    if (next === "live") {
      setLoading(true);
      void apiFetch<Connection[]>("/connections")
        .then((list) => setConnections(Array.isArray(list) ? list : []))
        .catch((e) => setError((e as Error).message))
        .finally(() => setLoading(false));
    } else {
      void load(query, 0);
    }
  }
  function filter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget),
      params = new URLSearchParams();
    for (const name of [
      "mailbox_id",
      "source_kind",
      "connection_id",
      "query",
      "sender",
      "subject",
      "from",
      "to",
    ]) {
      const value = String(data.get(name) || "").trim();
      if (value)
        params.set(
          name,
          name === "from" || name === "to"
            ? new Date(value).toISOString()
            : value,
        );
    }
    if (
      params.get("from") &&
      params.get("to") &&
      params.get("from")! >= params.get("to")!
    ) {
      setError("结束时间必须晚于开始时间。");
      return;
    }
    setNotice("");
    setQuery(params.toString());
    setOffset(0);
    void load(params.toString(), 0);
  }
  function liveFilter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!liveConnection) {
      setError("请先选择一个来源连接。");
      return;
    }
    const address = liveAddress.trim();
    if (!address) {
      setError("请填写要读取的邮箱地址。");
      return;
    }
    setError("");
    setNotice("");
    setOffset(0);
    void loadLive(liveConnection, address, liveQuery.trim(), 0);
  }
  function goPage(page: number) {
    setOffset(page);
    if (mode === "live")
      void loadLive(liveConnection, liveAddress.trim(), liveQuery.trim(), page);
    else void load(query, page);
  }
  async function open(id: string) {
    const request = ++detailRequest.current;
    setReading(true);
    setDetail(null);
    setError("");
    try {
      const message =
        mode === "live"
          ? await apiFetch<Message>(
              `/connections/${liveConnection}/live-mail/detail?address=${encodeURIComponent(
                liveAddress.trim(),
              )}&key=${encodeURIComponent(id)}`,
            )
          : await apiFetch<Message>(`/messages/${encodeURIComponent(id)}`);
      if (request === detailRequest.current) setDetail(message);
    } catch (e) {
      if (request === detailRequest.current) setError((e as Error).message);
    } finally {
      if (request === detailRequest.current) setReading(false);
    }
  }
  async function copy(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setNotice("验证码已复制");
    } catch {
      setError("复制失败，请手动选择验证码复制。");
    }
  }
  return (
    <section>
      <h1>统一收件箱</h1>
      <div className="modes" role="tablist" aria-label="收件箱数据源">
        <button
          role="tab"
          aria-selected={mode === "archive"}
          onClick={() => switchMode("archive")}
        >
          本地归档
        </button>
        <button
          role="tab"
          aria-selected={mode === "live"}
          onClick={() => switchMode("live")}
        >
          上游实时
        </button>
      </div>
      <p className="hint">
        {mode === "archive"
          ? "本地归档：已登记邮箱同步下来的邮件，支持全文搜索与来源筛选。"
          : "上游实时：用来源凭据直接读取任意地址的邮件，不必先登记；邮件不会写入本地归档。"}
      </p>
      {mode === "archive" ? (
        <form className="filters" onSubmit={filter}>
          <label>
            邮箱
            <select name="mailbox_id">
              <option value="">全部邮箱</option>
              {boxes.map((box) => (
                <option key={box.id} value={box.id}>
                  {box.address}
                </option>
              ))}
            </select>
          </label>
          <label>
            来源类型
            <select name="source_kind" defaultValue="">
              <option value="">全部来源</option>
              {Object.entries(providerLabels).map(([kind, label]) => (
                <option key={kind} value={kind}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            全文搜索
            <input name="query" placeholder="发件人、收件人、主题或正文" />
          </label>
          <label>
            发件人
            <input name="sender" />
          </label>
          <label>
            主题
            <input name="subject" />
          </label>
          <label>
            开始时间
            <input name="from" type="datetime-local" />
          </label>
          <label>
            结束时间（不含）
            <input name="to" type="datetime-local" />
          </label>
          <button disabled={loading}>筛选邮件</button>
        </form>
      ) : (
        <form className="filters" onSubmit={liveFilter}>
          <label>
            来源连接
            <select
              name="connection_id"
              value={liveConnection}
              onChange={(event) => {
                setLiveConnection(event.target.value);
                setLiveAddress("");
              }}
            >
              <option value="">选择连接</option>
              {connections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.name}（{providerLabels[connection.kind]}）
                </option>
              ))}
            </select>
          </label>
          <label>
            邮箱地址
            <input
              name="address"
              list="live-addresses"
              value={liveAddress}
              onChange={(event) => setLiveAddress(event.target.value)}
              placeholder="任意地址，不必先登记"
            />
          </label>
          <datalist id="live-addresses">
            {liveAddresses.map((address) => (
              <option key={address} value={address} />
            ))}
          </datalist>
          <label>
            实时搜索
            <input
              name="query"
              value={liveQuery}
              onChange={(event) => setLiveQuery(event.target.value)}
              placeholder="发件人、主题或摘要"
            />
          </label>
          <button disabled={loading}>读取邮件</button>
        </form>
      )}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <div className="inbox">
        <div aria-label="邮件列表" aria-busy={loading}>
          {loading ? (
            <p role="status">正在读取邮件…</p>
          ) : (
            <>
              <p>共 {total} 封邮件</p>
              {items.length === 0 ? (
                <p>
                  {mode === "live"
                    ? "上游没有返回邮件。确认地址填写正确，或该地址还没收到信。"
                    : "没有匹配的邮件。调整筛选条件，或到邮箱页执行同步。"}
                </p>
              ) : (
                <ul className="messages">
                  {items.map((item) => (
                    <li key={item.id}>
                      <button
                        className="subject"
                        onClick={() => void open(item.id)}
                      >
                        {item.subject || "（无主题）"}
                      </button>
                      <p>{item.sender}</p>
                      {item.source_kind && (
                        <p>
                          <span>来源：{providerLabels[item.source_kind]}</span>
                          {mode === "archive" && item.identity_status && (
                            <span> · {identityLabels[item.identity_status]}</span>
                          )}
                        </p>
                      )}
                      <time>{displayTime(item.received_at)}</time>
                      <p>{item.snippet}</p>
                      {item.verification_code != null && (
                        <div className="code">
                          <code>{item.verification_code}</code>
                          <button
                            onClick={() => void copy(item.verification_code!)}
                          >
                            复制验证码
                          </button>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              <div className="pagination">
                <button
                  disabled={offset === 0}
                  onClick={() => goPage(offset - 50)}
                >
                  上一页
                </button>
                <span>第 {Math.floor(offset / 50) + 1} 页</span>
                <button
                  disabled={offset + 50 >= total || offset >= 10000}
                  onClick={() => goPage(offset + 50)}
                >
                  下一页
                </button>
              </div>
            </>
          )}
        </div>
        <article className="reader" aria-busy={reading}>
          <h2 ref={heading} tabIndex={-1}>
            邮件详情
          </h2>
          {reading ? (
            <p role="status">正在读取正文…</p>
          ) : detail ? (
            <>
              <h3>{detail.subject || "（无主题）"}</h3>
              <p>发件人：{detail.sender}</p>
              <p>收件人：{detail.recipient}</p>
              <p>{displayTime(detail.received_at)}</p>
              {detail.link && (
                <p className="action-link">
                  行动链接：
                  <a
                    href={detail.link}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {detail.link}
                  </a>
                  <button onClick={() => void copy(detail.link!)}>
                    复制链接
                  </button>
                </p>
              )}
              <pre>{detail.body_text || "（无正文）"}</pre>
            </>
          ) : (
            <p>选择一封邮件阅读纯文本正文。</p>
          )}
        </article>
      </div>
    </section>
  );
}
