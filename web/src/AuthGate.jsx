import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import App from "./App.jsx";
import TeamApp from "./TeamApp.jsx";
import { requestJson } from "./ui-core.js";

export default function AuthGate() {
  const [auth, setAuth] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function checkSession() {
    setError("");
    try {
      const result = await requestJson(fetch, "/api/auth/status");
      setAuth(result);
    } catch (failure) {
      setError(failure.message);
    }
  }

  useEffect(() => {
    void checkSession();
  }, []);

  async function login(event) {
    event.preventDefault();
    if (busy) return;
    const values = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      await requestJson(fetch, "/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          username: values.get("username"),
          password: values.get("password"),
        }),
      });
      window.location.replace(window.location.pathname === '/team' ? '/team' : '/');
    } catch (failure) {
      setError(failure.message);
      setBusy(false);
    }
  }

  if (auth?.authenticated && auth.teams_enabled && window.location.pathname === '/team') return <TeamApp username={auth.username} />;
  if (auth?.authenticated) return <App teamsEnabled={auth.teams_enabled} authenticationEnabled={auth.enabled} username={auth.username || (auth.enabled ? "当前用户" : "本地用户")} />;

  return (
    <main className="login-page">
      <section className="login-card" aria-label="登录 DoTasks">
        <div className="login-brand">
          <img src="/dotasks-mark.svg" alt="" />
          <span>DoTasks</span>
        </div>
        {!auth ? (
          <div className="login-status">
            {error ? (
              <>
                <p role="alert">{error}</p>
                <Button variant="outline" onClick={checkSession}>
                  重新连接
                </Button>
              </>
            ) : (
              <p role="status">正在连接…</p>
            )}
          </div>
        ) : (
          <form onSubmit={login} className="login-form">
            <label htmlFor="login-username">
              账号
              <Input
                id="login-username"
                name="username"
                autoComplete="username"
                required
                autoFocus
                disabled={busy}
              />
            </label>
            <label htmlFor="login-password">
              密码
              <Input
                id="login-password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                disabled={busy}
              />
            </label>
            {error && (
              <p className="login-error" role="alert">
                {error}
              </p>
            )}
            <Button className="primary" type="submit" disabled={busy}>
              {busy ? "正在登录…" : "登录"}
            </Button>
          </form>
        )}
      </section>
    </main>
  );
}
