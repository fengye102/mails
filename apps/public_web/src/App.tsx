import { useEffect, useRef, useState } from "react";
import {
  apiFetch,
  displayTime,
  errorText,
  formatRemaining,
  type PublicMailbox,
  type PublicMessage,
} from "./api";

interface MessageList {
  items: PublicMessage[];
  total: number;
}

export function App() {
  const [mailbox, setMailbox] = useState<PublicMailbox | null>(null);
  const [messages, setMessages] = useState<PublicMessage[]>([]);
  const [detail, setDetail] = useState<PublicMessage | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const ticker = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!mailbox || mailbox.status !== "active") return;
    ticker.current = window.setInterval(() => {
      setMailbox((current) =>
        current
          ? {
              ...current,
              remaining_seconds: Math.max(0, current.remaining_seconds - 1),
              status: current.remaining_seconds <= 1 ? "expired" : current.status,
            }
          : current,
      );
    }, 1000);
    return () => window.clearInterval(ticker.current);
  }, [mailbox?.id, mailbox?.status]);

  async function createMailbox() {
    setBusy(true);
    setError("");
    setNotice("");
    setMessages([]);
    setDetail(null);
    try {
      setMailbox(await apiFetch<PublicMailbox>("/mailboxes", { method: "POST" }));
    } catch (caught) {
      setMailbox(null);
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function refreshMessages() {
    if (!mailbox) return;
    setLoadingMessages(true);
    setError("");
    setNotice("");
    try {
      const result = await apiFetch<MessageList>(
        `/mailboxes/${encodeURIComponent(mailbox.id)}/messages`,
      );
      setMessages(result.items);
      setMailbox((current) =>
        current ? { ...current, last_accessed_at: new Date().toISOString() } : current,
      );
    } catch (caught) {
      setError((caught as Error).message);
      if ((caught as Error).message === errorText("PUBLIC_MAILBOX_EXPIRED")) {
        setMailbox((current) => (current ? { ...current, status: "expired", remaining_seconds: 0 } : current));
      }
    } finally {
      setLoadingMessages(false);
    }
  }

  async function openMessage(id: string) {
    if (!mailbox) return;
    setError("");
    try {
      setDetail(
        await apiFetch<PublicMessage>(
          `/mailboxes/${encodeURIComponent(mailbox.id)}/messages/${encodeURIComponent(id)}`,
        ),
      );
    } catch (caught) {
      setError((caught as Error).message);
    }
  }

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setNotice(`${label}已复制`);
      setError("");
    } catch {
      setError("复制失败，请手动选择内容复制。");
    }
  }

  return (
    <div className="public-shell">
      <header className="public-header">
        <div>
          <p className="eyebrow">MAILS / PUBLIC INBOX</p>
          <h1>临时邮箱</h1>
          <p>无需注册，创建一个短期收件地址。</p>
        </div>
        <button className="primary" disabled={busy} onClick={() => void createMailbox()}>
          {busy ? "创建中…" : mailbox ? "创建新的邮箱" : "创建临时邮箱"}
        </button>
      </header>

      <main>
        {error && <p role="alert" className="alert">{error}</p>}
        {notice && <p role="status" className="notice">{notice}</p>}
        {!mailbox ? (
          <section className="empty-card" aria-live="polite">
            <div className="empty-icon" aria-hidden="true">@</div>
            <h2>先创建一个临时邮箱</h2>
            <p>邮件只在有效期内保留。请勿用于重要账号或敏感信息。</p>
            <button className="primary large" disabled={busy} onClick={() => void createMailbox()}>
              {busy ? "创建中…" : "创建临时邮箱"}
            </button>
          </section>
        ) : (
          <section className="workspace">
            <article className="mailbox-card">
              <div>
                <p className="eyebrow">YOUR TEMPORARY ADDRESS</p>
                <p className="address">{mailbox.address}</p>
                <p className={mailbox.status === "active" ? "ttl" : "ttl expired"}>
                  {mailbox.status === "active" ? formatRemaining(mailbox.remaining_seconds) : "已过期"}
                </p>
              </div>
              <div className="actions">
                <button onClick={() => void copy(mailbox.address, "邮箱地址")}>复制邮箱地址</button>
                <button onClick={() => void refreshMessages()} disabled={loadingMessages || mailbox.status !== "active"}>
                  {loadingMessages ? "刷新中…" : "刷新收件箱"}
                </button>
              </div>
            </article>

            <div className="inbox-layout">
              <section className="messages-card" aria-label="邮件列表" aria-busy={loadingMessages}>
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">INBOX</p>
                    <h2>收件箱</h2>
                  </div>
                  <span>{messages.length} 封</span>
                </div>
                {messages.length === 0 ? (
                  <div className="no-messages">
                    <p>还没有新邮件</p>
                    <span>点击“刷新收件箱”检查新消息。</span>
                  </div>
                ) : (
                  <ul className="message-list">
                    {messages.map((message) => (
                      <li key={message.id}>
                        <button className="message-row" onClick={() => void openMessage(message.id)}>
                          <span className="message-subject">{message.subject || "（无主题）"}</span>
                          <span className="message-meta">{message.sender} · {displayTime(message.received_at)}</span>
                          <span className="message-snippet">{message.snippet}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <article className="detail-card">
                <p className="eyebrow">MESSAGE</p>
                <h2>{detail?.subject || "邮件详情"}</h2>
                {detail ? (
                  <>
                    <p className="detail-meta">来自 {detail.sender}</p>
                    <time>{displayTime(detail.received_at)}</time>
                    {detail.verification_code && (
                      <div className="verification">
                        <span>验证码</span>
                        <code>{detail.verification_code}</code>
                        <button onClick={() => void copy(detail.verification_code!, "验证码")}>复制</button>
                      </div>
                    )}
                    <pre>{detail.body_text || "（无正文）"}</pre>
                  </>
                ) : (
                  <p className="muted">选择一封邮件查看纯文本内容。</p>
                )}
              </article>
            </div>
          </section>
        )}
      </main>
      <footer>临时邮箱会自动过期 · 仅供接收邮件</footer>
    </div>
  );
}
