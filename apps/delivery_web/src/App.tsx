import { useEffect, useRef, useState } from "react";
import {
  displayTime,
  errorText,
  formatRemaining,
  readMailbox,
  verifyPickup,
  verifyPickupKey,
  type DeliveryMailbox,
  type PickupSession,
} from "./api";

/** 取件链接形如 `/pickup#email=..&key=..`：密钥放在 fragment 里，不会进服务端日志。 */
export function readFragmentCredentials(
  hash: string,
): { key: string; email: string } | null {
  const raw = hash.replace(/^#/, "").trim();
  if (!raw) return null;
  const params = new URLSearchParams(raw);
  const key = (params.get("key") ?? "").trim();
  if (!key) return null;
  return { key, email: (params.get("email") ?? "").trim() };
}

export function App() {
  const [code, setCode] = useState("");
  const [session, setSession] = useState<PickupSession | null>(null);
  const [mailbox, setMailbox] = useState<DeliveryMailbox | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [remaining, setRemaining] = useState(0);
  const [linked, setLinked] = useState(false);
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

  async function open(credential: { code?: string; key?: string; email?: string }) {
    setBusy(true);
    setError("");
    setNotice("");
    setMailbox(null);
    try {
      const opened = credential.key
        ? await verifyPickupKey(credential.key, credential.email)
        : await verifyPickup(credential.code ?? "");
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

  // 打开取件链接即取件：读完 fragment 立刻把它从地址栏清掉，避免密钥留在历史记录/截图里。
  useEffect(() => {
    const credential = readFragmentCredentials(window.location.hash);
    if (!credential) return;
    window.history.replaceState({}, "", window.location.pathname);
    setLinked(true);
    void open(credential);
    // 只在首次挂载时读取链接，之后交给表单
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = code.trim();
    if (!trimmed) {
      setError("请输入取件码。");
      return;
    }
    await open({ code: trimmed });
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
    setLinked(false);
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
          <p>输入取件码或直接打开取件链接，领取分配给你的邮箱和其中的邮件。</p>
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
            {linked && busy && <p className="muted">正在通过取件链接进入…</p>}
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
                <p className="muted">
                  {mailbox
                    ? `已入库 ${mailbox.sync.messages_total} 封${
                        mailbox.sync.last_ingested_at
                          ? ` · 最近更新 ${displayTime(mailbox.sync.last_ingested_at)}`
                          : " · 暂无入库记录"
                      }`
                    : "读取中…"}
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
                        {message.link && (
                          <div className="action-link">
                            <span>行动链接</span>
                            <a href={message.link} target="_blank" rel="noreferrer noopener">
                              {message.link}
                            </a>
                            <button onClick={() => void copy(message.link ?? "", "行动链接")}>
                              复制链接
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
      <footer>取件会话到期后自动失效 · 取件码与密钥不落盘保存</footer>
    </div>
  );
}
