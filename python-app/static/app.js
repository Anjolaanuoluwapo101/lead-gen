/* Shared browser plumbing: session, API calls, formatting.
 *
 * The token is held in sessionStorage, not memory. In-memory would be stricter,
 * but it dies on every page navigation, and the dashboard is five pages — the
 * constant re-login would be the thing users noticed. sessionStorage is
 * same-origin, cleared when the tab closes, and never sent anywhere except to
 * Supabase Auth and to our own API.
 */
(function () {
  "use strict";

  var CFG = window.LEADGEN || {};

  function token() {
    return sessionStorage.getItem("leadgen_token") || "";
  }

  function setSession(access, email) {
    sessionStorage.setItem("leadgen_token", access);
    if (email) sessionStorage.setItem("leadgen_email", email);
  }

  function clearSession() {
    sessionStorage.removeItem("leadgen_token");
    sessionStorage.removeItem("leadgen_email");
  }

  function email() {
    return sessionStorage.getItem("leadgen_email") || "";
  }

  function requireLogin() {
    if (!token() && CFG.authRequired) {
      window.location.href = "/login";
      return true;
    }
    return false;
  }

  /* api() is the only way the pages talk to the backend.
   *
   * A 401 clears the session and bounces to /login rather than showing an
   * error: an expired token is not something the user can act on in place, and
   * a raw "invalid or expired token" in a red box is just confusing.
   */
  async function api(path, options) {
    options = options || {};
    var headers = Object.assign({ "Content-Type": "application/json" },
                                options.headers || {});
    var t = token();
    if (t) headers["Authorization"] = "Bearer " + t;

    var resp = await fetch(path, {
      method: options.method || (options.body ? "POST" : "GET"),
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    });

    if (resp.status === 401) {
      clearSession();
      window.location.href = "/login";
      throw new Error("not signed in");
    }
    var data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      var msg = (data && data.error) || ("request failed (" + resp.status + ")");
      var err = new Error(msg);
      err.status = resp.status;
      err.body = data;
      throw err;
    }
    return data;
  }

  /* --- Supabase Auth, straight from the browser ---------------------------
   * The password never reaches our server. Supabase returns a JWT that we then
   * present to our own API, which verifies it against Supabase on every call
   * (see auth.py). Our server never sees or stores the password.
   */
  async function authRequest(path, body) {
    if (!CFG.supabaseUrl || !CFG.anonKey) {
      throw new Error("Supabase Auth is not configured on the server: set "
                    + "SUPABASE_URL and SUPABASE_ANON_KEY.");
    }
    var resp = await fetch(CFG.supabaseUrl + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "apikey": CFG.anonKey },
      body: JSON.stringify(body)
    });
    var data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (!resp.ok) {
      throw new Error((data && (data.msg || data.error_description ||
                                data.error)) || "authentication failed");
    }
    return data;
  }

  function signup(addr, password) {
    return authRequest("/auth/v1/signup", { email: addr, password: password });
  }

  async function login(addr, password) {
    var data = await authRequest("/auth/v1/token?grant_type=password",
                                 { email: addr, password: password });
    if (!data || !data.access_token) {
      throw new Error("no access token returned. Is the Email provider "
                    + "enabled in Supabase?");
    }
    setSession(data.access_token, (data.user && data.user.email) || addr);
    return data;
  }

  function logout() {
    clearSession();
    window.location.href = "/login";
  }

  /* --- small helpers ----------------------------------------------------- */
  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function showError(node, message) {
    if (!node) return;
    node.textContent = message;
    node.hidden = !message;
  }

  function when(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }

  function money(n) {
    return "$" + (Number(n) || 0).toFixed(4);
  }

  /* Every page shows who is signed in, and the nav's sign-out button only
   * exists when there is something to sign out of. */
  document.addEventListener("DOMContentLoaded", function () {
    var out = el("logout");
    var who = el("whoami");
    if (out) {
      out.hidden = !token();
      out.addEventListener("click", logout);
    }
    if (who) who.textContent = email();
  });

  window.LEADGEN_APP = {
    token: token, api: api, login: login, signup: signup, logout: logout,
    requireLogin: requireLogin, el: el, esc: esc, showError: showError,
    when: when, money: money, cfg: CFG
  };
})();
