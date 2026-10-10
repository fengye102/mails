import { useState, type FormEvent } from "react";
import { apiFetch, type Session } from "../api";
export function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    const body = JSON.stringify({
      username: data.get("username"),
      password: data.get("password"),
    });
    (form.elements.namedItem("password") as HTMLInputElement).value = "";
    try {
      onLogin(await apiFetch<Session>("/session", { method: "POST", body }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login">
      <h1>Mails 私有邮箱</h1>
      <p>使用内部管理员账号登录。</p>
      <form onSubmit={submit}>
        <label>
          用户名
          <input name="username" autoComplete="username" required autoFocus />
        </label>
        <label>
          密码
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        <button disabled={busy}>{busy ? "登录中…" : "登录"}</button>
      </form>
      {error && <p role="alert">{error}</p>}
    </main>
  );
}
