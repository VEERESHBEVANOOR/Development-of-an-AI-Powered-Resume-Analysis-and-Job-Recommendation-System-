(function () {
    function mk(tag, cls, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text) e.textContent = text;
        return e;
    }

    const launcher = mk("button", "raya-launcher");
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open Raya assistant");
    launcher.innerHTML = '<span class="raya-face">🦾</span><span class="raya-pulse"></span>';

    const panel = mk("div", "raya-panel");
    const header = mk("div", "raya-header");
    header.appendChild(mk("strong", "", "Raya"));
    const headerActions = mk("div", "raya-header-actions");
    const minBtn = mk("button", "raya-min", "−");
    minBtn.type = "button";
    minBtn.title = "Minimize";
    const expandBtn = mk("button", "raya-expand", "⛶");
    expandBtn.type = "button";
    expandBtn.title = "Maximize";
    const closeBtn = mk("button", "raya-close", "×");
    closeBtn.type = "button";
    headerActions.appendChild(minBtn);
    headerActions.appendChild(expandBtn);
    headerActions.appendChild(closeBtn);
    header.appendChild(headerActions);

    const messages = mk("div", "raya-messages");
    const intro = mk("div", "raya-msg raya-bot");
    intro.textContent = "Hi, I am Raya. Ask me anything about this app or your resume.";
    messages.appendChild(intro);
    const history = [{ role: "assistant", content: intro.textContent }];

    const suggestionsWrap = mk("div", "raya-suggestions");
    const suggestions = [
        "How do I upload my resume?",
        "Which skills should I improve first?",
        "Give me 3 project ideas based on my profile",
        "How can I improve my ATS score?",
        "Create a 7-day interview preparation plan",
    ];
    suggestions.forEach((s) => {
        const chip = mk("button", "raya-suggestion", s);
        chip.type = "button";
        chip.addEventListener("click", function () {
            suggestionsWrap.classList.add("hidden");
            input.value = s;
            form.requestSubmit();
        });
        suggestionsWrap.appendChild(chip);
    });

    const form = mk("form", "raya-form");
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Ask Raya...";
    input.required = true;
    const send = mk("button", "", "Send");
    send.type = "submit";
    form.appendChild(input);
    form.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(messages);
    panel.appendChild(suggestionsWrap);
    panel.appendChild(form);
    document.body.appendChild(launcher);
    document.body.appendChild(panel);
    let selectedUploadId = null;
    let pendingQuestion = "";
    let lastQuestion = "";

    function pushMsg(text, who, record = true) {
        const node = mk("div", "raya-msg " + (who === "user" ? "raya-user" : "raya-bot"));
        node.textContent = text;
        messages.appendChild(node);
        messages.scrollTop = messages.scrollHeight;
        if (record && text) {
            history.push({ role: who === "user" ? "user" : "assistant", content: text });
            if (history.length > 16) history.splice(0, history.length - 16);
        }
        return node;
    }

    function pushResumeOptions(options) {
        const wrap = mk("div", "raya-resume-options");
        (options || []).forEach((opt) => {
            const b = mk("button", "raya-resume-option", `${opt.filename} (${opt.created_at})`);
            b.type = "button";
            b.addEventListener("click", function () {
                selectedUploadId = opt.id;
                pushMsg(`Use this resume: ${opt.filename}`, "user");
                wrap.remove();
                if (pendingQuestion) {
                    askRaya(pendingQuestion, false);
                }
            });
            wrap.appendChild(b);
        });
        messages.appendChild(wrap);
        messages.scrollTop = messages.scrollHeight;
    }

    async function askRaya(text, showUserMessage) {
        if (!text) return;
        if (showUserMessage) pushMsg(text, "user");
        const contextRef = lastQuestion;
        const currentQuestion = text.trim();
        suggestionsWrap.classList.add("hidden");
        send.disabled = true;
        const typingNode = pushMsg("Raya is typing...", "bot", false);
        typingNode.classList.add("raya-typing");
        const startedAt = Date.now();
        try {
            const resp = await fetch("/raya-chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message: text,
                    context_question: contextRef,
                    path: window.location.pathname || "/",
                    selected_upload_id: selectedUploadId,
                    history: history.slice(-12),
                }),
            });
            const data = await resp.json();
            const elapsed = Date.now() - startedAt;
            if (elapsed < 3000) {
                await new Promise((resolve) => setTimeout(resolve, 3000 - elapsed));
            }
            typingNode.remove();
            pushMsg((data && data.reply) || "Sorry, I could not respond.", "bot");
            if (data && data.needs_selection && Array.isArray(data.options)) {
                pendingQuestion = text;
                pushResumeOptions(data.options);
            } else {
                pendingQuestion = "";
            }
        } catch (_) {
            const elapsed = Date.now() - startedAt;
            if (elapsed < 3000) {
                await new Promise((resolve) => setTimeout(resolve, 3000 - elapsed));
            }
            typingNode.remove();
            pushMsg("Connection issue. Please try again.", "bot");
        } finally {
            send.disabled = false;
            suggestionsWrap.classList.remove("hidden");
            if (currentQuestion) lastQuestion = currentQuestion;
            input.focus();
        }
    }

    launcher.addEventListener("click", function () {
        panel.classList.toggle("show");
        if (panel.classList.contains("show")) input.focus();
    });

    closeBtn.addEventListener("click", function () {
        panel.classList.remove("show");
    });

    minBtn.addEventListener("click", function () {
        panel.classList.remove("show");
    });

    expandBtn.addEventListener("click", function () {
        panel.classList.toggle("raya-panel-max");
        expandBtn.textContent = panel.classList.contains("raya-panel-max") ? "🗗" : "⛶";
        expandBtn.title = panel.classList.contains("raya-panel-max") ? "Restore" : "Maximize";
    });

    form.addEventListener("submit", async function (e) {
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        pushMsg(text, "user");
        input.value = "";
        await askRaya(text, false);
    });
})();
