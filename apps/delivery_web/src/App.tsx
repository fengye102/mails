import { useEffect, useRef, useState } from "react";
import {
  displayTime,
  errorText,
  formatRemaining,
  readMailbox,
  verifyPickup,
  type DeliveryMailbox,
  type PickupSession,
} from "./api";

export function App() {
  const [code, setCode] = useState("");
  const [session, setSession] = useState<PickupSession | null>(null);
  const [mailbox, setMailbox] = useState<DeliveryMailbox | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [remaining, setRemaining] = useState(0);
  const ticker = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (!session) return;
    const deadline = new Date(session.expires_at).getTime();
    const update = () =>
      setRemaining(Math.max(0, Math.round((deadline - Date.now()) / 1000)));
    update();
    ticker.current = window.setInterval(update, 1000);
    return () => window.clearInterval(ticker.current);
  }, [session?.session_token]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = code.trim();
    if (!trimmed) {
      setError("请输入取件码。");
      return;
    }
    setBusy(true);
    setError("");
    setNotice("");
    setMailbox(null);
    try {
      const opened = await verifyPickup(trimmed);
      setSession(opened);
      setMailbox(await readMailbox(opened.allocation_id, opened.session_token));
      setCode("");
    } catch (caught) {
      setSession(null);
      setError((caught as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    if (!session) return;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      setMailbox(await readMailbox(session.allocation_id, session.session_token));
    } catch (caught) {
      setError((caught as Error).message);
      if ((caught as Error).message === errorText("DELIVERY_PICKUP_SESSION_INVALID")) {
        closeSession();
      }
    } finally {
      setLoading(false);
    }
  }

  function closeSession() {
    setSession(null);
    setMailbox(null);
    setCode("");
    setNotice("");
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
    <div className="delivery-shell">
      <header className="delivery-header">
        <div>
          <p className="eyebrow">MAILS / PICKUP</p>
          <h1>取件</h1>
          <p>输入取件码，领取分配给你的邮箱和其中的邮件。</p>
        </div>
        {session && (
          <button className="ghost" onClick={closeSession}>
            退出取件
          </button>
        )}
      </header>

      <main>
        {error && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}
        {notice && (
          <p role="status" className="notice">
            {notice}
          </p>
        )}

        {!session ? (
          <section className="pickup-card">
            <p className="eyebrow">PICKUP CODE</p>
            <h2>输入取件码</h2>
            <p className="muted">
              取件码一次性有效；验证成功后会立即失效，请勿转发给他人。
            </p>
            <form onSubmit={(event) => void submit(event)}>
              <label htmlFor="pickup-code">取件码</label>
              <input
                id="pickup-code"
                name="pickup_code"
                autoComplete="off"
                spellCheck={false}
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder="例如 7F3K-92QD-8M1Z"
              />
              <button className="primary" type="submit" disabled={busy}>
                {busy ? "验证中…" : "取件"}
              </button>
            </form>
          </section>
        ) : (
          <section className="workspace">
            <article className="mailbox-card">
              <div>
                <p className="eyebrow">DELIVERED MAILBOX</p>
                <p className="address">{mailbox?.address ?? "读取中…"}</p>
                <p className={remaining > 0 ? "ttl" : "ttl expired"}>
                  {remaining > 0 ? `会话${formatRemaining(remaining)}` : "会话已过期"}
                </p>
              </div>
              <div className="actions">
                <button
                  onClick={() => void copy(mailbox?.address ?? "", "邮箱地址")}
                  disabled={!mailbox}
                >
                  复制邮箱地址
                </button>
                <button onClick={() => void refresh()} disabled={loading}>
                  {loading ? "刷新中…" : "刷新邮件"}
                </button>
              </div>
            </article>

            <section className="messages-card" aria-label="邮件列表" aria-busy={loading}>
              <div className="section-heading">
                <div>
                  <p className="eyebrow">INBOX</p>
                  <h2>邮件</h2>
                </div>
                <span>{mailbox?.messages.length ?? 0} 封</span>
              </div>
              {!mailbox || mailbox.messages.length === 0 ? (
                <div className="no-messages">
                  <p>还没有邮件</p>
                  <span>收到新邮件后点击“刷新邮件”。</span>
                </div>
              ) : (
                <ul className="message-list">
                  {mailbox.messages.map((message) => (
                    <li key={message.id}>
                      <article className="message-item">
                        <header>
                          <span className="message-subject">
                            {message.subject || "（无主题）"}
                          </span>
                          <span className="message-meta">
                            {message.sender} · {displayTime(message.received_at)}
                          </span>
                        </header>
                        {message.verification_code && (
                          <div className="verification">
                            <span>验证码</span>
                            <code>{message.verification_code}</code>
                            <button
                              onClick={() =>
                                void copy(message.verification_code ?? "", "验证码")
                              }
                            >
                              复制
                            </button>
                          </div>
                        )}
                        <pre>{message.body_text || "（无正文）"}</pre>
                      </article>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </section>
        )}
      </main>
      <footer>取件会话到期后自动失效 · 取件码不落盘保存</footer>
    </div>
  );
}
