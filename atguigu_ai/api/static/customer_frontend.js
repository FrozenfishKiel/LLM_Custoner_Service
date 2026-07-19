(function () {
  const state = {
    identity: null,
    pending: false,
  };

  const qs = (selector) => document.querySelector(selector);
  const qsa = (selector) => Array.from(document.querySelectorAll(selector));

  function cookie(name) {
    return document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(`${name}=`))
      ?.slice(name.length + 1) || "";
  }

  function formJson(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  function setNotice(message, kind = "info") {
    const node = qs("#notice");
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("is-error", kind === "error");
    node.classList.toggle("is-success", kind === "success");
  }

  function setAuthenticated(identity) {
    state.identity = identity;
    document.body.classList.toggle("is-authenticated", Boolean(identity));
    qs("#accountStatus").textContent = identity
      ? `${identity.role} · ${identity.status}`
      : "未登录";
  }

  function friendlyError(status, detail) {
    if (status === 401) return "请先登录后再继续。";
    if (status === 403) return "安全校验失败，请刷新页面后重试。";
    if (status === 409) return "账户还没有绑定业务用户，请联系管理员处理。";
    if (status === 429) return "操作太频繁了，请稍后再试。";
    if (status === 503) return "服务暂时不可用，请稍后重试。";
    if (detail) return String(detail);
    return "请求失败，请检查输入后重试。";
  }

  async function api(path, options = {}) {
    const headers = {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    };
    const csrf = cookie("auth_csrf");
    if (csrf) headers["X-CSRF-Token"] = csrf;
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers,
    });
    if (!response.ok) {
      let detail = "";
      try {
        detail = (await response.json()).detail;
      } catch (_) {
        detail = response.statusText;
      }
      throw new Error(friendlyError(response.status, detail));
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function addMessage(text, role = "bot") {
    const messages = qs("#messages");
    if (!messages) return;
    const item = document.createElement("div");
    item.className = `message ${role}`;
    item.textContent = text;
    messages.appendChild(item);
    messages.scrollTop = messages.scrollHeight;
  }

  async function submitJson(form, path, successMessage) {
    if (state.pending) return null;
    state.pending = true;
    setNotice("正在处理请求...");
    try {
      const result = await api(path, {
        method: "POST",
        body: JSON.stringify(formJson(form)),
      });
      setNotice(successMessage, "success");
      return result;
    } catch (error) {
      setNotice(error.message, "error");
      throw error;
    } finally {
      state.pending = false;
    }
  }

  function bindForms() {
    qs("#loginForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const identity = await submitJson(event.currentTarget, "/api/auth/login", "登录成功，可以开始客服会话。");
        setAuthenticated(identity);
      } catch (_) {}
    });

    qs("#registerForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await submitJson(event.currentTarget, "/api/auth/register", "注册请求已接收，请检查验证邮件。");
      } catch (_) {}
    });

    qs("#forgotPasswordForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await submitJson(event.currentTarget, "/api/auth/forgot-password", "如果邮箱存在，重置邮件会发送到该邮箱。");
      } catch (_) {}
    });

    qs("#resetPasswordForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await submitJson(event.currentTarget, "/api/auth/reset-password", "密码重置请求已处理。");
      } catch (_) {}
    });

    qs("#changePasswordForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await submitJson(event.currentTarget, "/api/auth/change-password", "密码已修改，请重新登录。");
        setAuthenticated(null);
      } catch (_) {}
    });

    qs("#chatForm")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = qs("#chatInput");
      const message = input.value.trim();
      if (!message) return;
      addMessage(message, "user");
      input.value = "";
      setNotice("AI 售后顾问正在回复...");
      try {
        const replies = await api("/api/chat/messages", {
          method: "POST",
          body: JSON.stringify({ message }),
        });
        if (!replies.length) addMessage("我没有收到可展示的回复。");
        replies.forEach((reply) => addMessage(reply.text || "收到，我会继续为你处理。"));
        setNotice("消息已发送。", "success");
      } catch (error) {
        addMessage(error.message, "error");
        setNotice(error.message, "error");
      }
    });
  }

  function bindButtons() {
    qs("#logoutButton")?.addEventListener("click", async () => {
      try {
        await api("/api/auth/logout", { method: "POST", body: "{}" });
        setAuthenticated(null);
        setNotice("已退出登录。", "success");
      } catch (error) {
        setNotice(error.message, "error");
      }
    });

    qs("#resetChatButton")?.addEventListener("click", async () => {
      try {
        await api("/api/chat/reset", { method: "POST", body: "{}" });
        qs("#messages").innerHTML = "";
        addMessage("会话已重置。你可以重新描述订单或售后问题。");
        setNotice("聊天已重置。", "success");
      } catch (error) {
        setNotice(error.message, "error");
      }
    });

    qs("#refreshAccountButton")?.addEventListener("click", refreshAccount);

    qsa("[data-chat-prompt]").forEach((button) => {
      button.addEventListener("click", () => {
        qs("#chatInput").value = button.dataset.chatPrompt;
        qs("#chatInput").focus();
      });
    });
  }

  async function refreshAccount() {
    try {
      const identity = await api("/api/account/me");
      setAuthenticated(identity);
      setNotice("账户状态已刷新。", "success");
    } catch (error) {
      setAuthenticated(null);
      setNotice(error.message, "error");
    }
  }

  function init() {
    bindForms();
    bindButtons();
    refreshAccount();
  }

  window.CustomerFrontend = {
    init,
    api,
    friendlyError,
    cookie,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
