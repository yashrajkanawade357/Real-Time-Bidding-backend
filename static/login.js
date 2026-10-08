// Log in / create an account. The server sets an HttpOnly session cookie;
// this page never sees or stores the session itself.
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  // Only ever send people back to a page on this site.
  const wanted = params.get("next") || "/floor";
  const next = wanted.startsWith("/") && !wanted.startsWith("//") ? wanted : "/floor";

  function showTab(signup) {
    $("tabLogin").setAttribute("aria-selected", String(!signup));
    $("tabSignup").setAttribute("aria-selected", String(signup));
    $("loginForm").hidden = signup;
    $("signupForm").hidden = !signup;
    $("authTitle").textContent = signup ? "Create your account" : "Log in to bid";
    $("authError").textContent = "";
    (signup ? $("signupForm") : $("loginForm")).querySelector("input").focus();
  }
  $("tabLogin").addEventListener("click", () => showTab(false));
  $("tabSignup").addEventListener("click", () => showTab(true));

  function message(status, body) {
    if (status === 422) {
      const field = body && body.detail && body.detail[0] && body.detail[0].loc ? body.detail[0].loc.at(-1) : "";
      if (field === "password") return "Passwords need at least 8 characters.";
      if (field === "display_name") return "Display names are 3-24 characters.";
      return "Please check the details and try again.";
    }
    return (body && typeof body.detail === "string") ? body.detail : "Something went wrong. Try again.";
  }

  async function submit(form, path) {
    const button = form.querySelector("button[type=submit]");
    const data = Object.fromEntries(new FormData(form));
    button.disabled = true;
    $("authError").textContent = "";
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        $("authError").textContent = message(res.status, body);
        return;
      }
      location.href = next;
    } catch {
      $("authError").textContent = "Couldn't reach the server. Check your connection.";
    } finally {
      button.disabled = false;
    }
  }

  $("loginForm").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/auth/login"); });
  $("signupForm").addEventListener("submit", (e) => { e.preventDefault(); submit(e.target, "/auth/signup"); });

  $("logoutBtn").addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST" });
    location.reload();
  });

  async function boot() {
    let me = null;
    try { me = (await (await fetch("/auth/me")).json()).user; } catch { /* treat as signed out */ }
    if (me) {
      document.querySelector(".auth-tabs").hidden = true;
      $("loginForm").hidden = true;
      $("signupForm").hidden = true;
      $("signedIn").hidden = false;
      $("signedInName").textContent = me.display_name;
      $("continueLink").href = next;
      $("authTitle").textContent = "Welcome back";
      $("authLede").textContent = me.email;
      return;
    }
    showTab(params.get("mode") === "signup");
  }
  boot();
})();
