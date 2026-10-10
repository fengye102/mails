import { useEffect, useState } from "react";
import { apiFetch, type Session } from "./api";
import { Login } from "./components/Login";
import { Connections } from "./components/Connections";
import { Mailboxes } from "./components/Mailboxes";
import { Inbox } from "./components/Inbox";
export function App() {
  const [session, setSession] = useState<Session | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(""),
    [page, setPage] = useState("收件箱"),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const logout = () => {
      setSession(null);
      setPage("收件箱");
    };
    window.addEventListener("mails:unauthorized", logout);
    void apiFetch<Session>("/session")
      .then((value) => {
        if (active) setSession(value);
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      window.removeEventListener("mails:unauthorized", logout);
    };
  }, []);
  async function logout() {
    setBusy(true);
    setError("");
    try {
      await apiFetch("/session", { method: "DELETE" }, session?.csrf_token);
      setSession(null);
      setPage("收件箱");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  if (loading)
    return (
      <main>
        <p role="status">正在恢复会话…</p>
      </main>
    );
  if (!session) return <Login onLogin={setSession} />;
  return (
    <div className="shell">
      <a className="skip" href="#content">
        跳到内容
      </a>
      <header>
        <strong>Mails</strong>
        <p>私有邮箱工作台</p>
        <nav aria-label="主导航">
          {["收件箱", "邮箱", "来源连接"].map((name) => (
            <button
              key={name}
              aria-current={page === name ? "page" : undefined}
              onClick={() => setPage(name)}
            >
              {name}
            </button>
          ))}
        </nav>
        <p>{session.user.username}</p>
        <button disabled={busy} onClick={() => void logout()}>
          退出登录
        </button>
      </header>
      <main id="content" tabIndex={-1}>
        {error && <p role="alert">{error}</p>}
        {page === "收件箱" ? (
          <Inbox />
        ) : page === "邮箱" ? (
          <Mailboxes csrfToken={session.csrf_token} />
        ) : (
          <Connections csrfToken={session.csrf_token} />
        )}
      </main>
    </div>
  );
}
