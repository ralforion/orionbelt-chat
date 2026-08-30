// Replace Chainlit's default favicon with the OrionBelt mark
(function setFavicon() {
  var HREF = "/public/favicon.png";
  function apply() {
    document.querySelectorAll("link[rel~='icon']").forEach(function (l) { l.remove(); });
    var link = document.createElement("link");
    link.rel = "icon";
    link.type = "image/png";
    link.href = HREF;
    document.head.appendChild(link);
  }
  apply();
  // Chainlit may re-inject its own <link> after hydration, so re-apply if it does
  new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var added = muts[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var n = added[j];
        if (n.tagName === "LINK" && /icon/i.test(n.rel || "") && n.href.indexOf(HREF) === -1) {
          apply();
          return;
        }
      }
    }
  }).observe(document.head, { childList: true });
})();

// Inject OrionBelt logo, app name, and version badge into the Chainlit header
(function injectHeader() {
  var VERSION = "v1.5.0";
  var LOGO_DARK = "/public/logo_w.png";
  var LOGO_LIGHT = "/public/logo.png";
  // Rendered next to the OrionBelt wordmark, so this reads
  // "OrionBelt Chat – AI Assistant". The suffix is the AI Act Art. 50(1)
  // disclosure in the UI chrome — keep it.
  var APP_NAME = "Chat – AI Assistant";

  function insert() {
    if (document.querySelector(".orionbelt-header-brand")) return true;

    var allAnchors = document.querySelectorAll("a");
    var headerAnchor = null;
    for (var i = 0; i < allAnchors.length; i++) {
      var text = allAnchors[i].textContent.trim();
      if (text === "GitHub" || text === "Report Issue" || text === "Readme") {
        headerAnchor = allAnchors[i];
        break;
      }
    }

    if (!headerAnchor) return false;

    var headerBar = headerAnchor.parentElement;
    while (headerBar && headerBar.parentElement && headerBar.parentElement.id !== "root") {
      if (headerBar.offsetWidth > window.innerWidth * 0.8) break;
      headerBar = headerBar.parentElement;
    }

    headerBar.style.position = "relative";

    var brand = document.createElement("div");
    brand.className = "orionbelt-header-brand";

    var isDark = document.documentElement.classList.contains("dark") ||
      window.matchMedia("(prefers-color-scheme: dark)").matches;

    var logo = document.createElement("img");
    logo.src = isDark ? LOGO_DARK : LOGO_LIGHT;
    logo.alt = "OrionBelt";
    logo.className = "orionbelt-header-logo";

    // Switch logo when theme changes
    new MutationObserver(function () {
      var dark = document.documentElement.classList.contains("dark");
      logo.src = dark ? LOGO_DARK : LOGO_LIGHT;
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

    var appName = document.createElement("span");
    appName.className = "orionbelt-header-name";
    appName.textContent = APP_NAME;

    brand.appendChild(logo);
    brand.appendChild(appName);
    headerBar.appendChild(brand);

    var linksParent = headerAnchor.parentElement;
    var badge = document.createElement("span");
    badge.className = "orionbelt-version";
    badge.textContent = VERSION;
    linksParent.insertBefore(badge, linksParent.firstChild);

    return true;
  }

  if (!insert()) {
    var observer = new MutationObserver(function () {
      if (insert()) observer.disconnect();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(function () { observer.disconnect(); }, 15000);
  }
})();

// Inject the copyright / trademark footer at the bottom of the page
(function injectFooter() {
  function insert() {
    if (document.querySelector(".orionbelt-footer")) return true;
    if (!document.body) return false;

    var footer = document.createElement("footer");
    footer.className = "orionbelt-footer";

    var copyright = document.createElement("span");
    copyright.className = "orionbelt-footer-line";
    copyright.textContent = "Copyright © 2026 RALFORION d.o.o.";

    var trademark = document.createElement("span");
    trademark.className = "orionbelt-footer-line";
    trademark.textContent = "OrionBelt® is a registered trademark of RALFORION d.o.o.";

    footer.appendChild(copyright);
    footer.appendChild(trademark);
    document.body.appendChild(footer);

    return true;
  }

  if (!insert()) {
    var observer = new MutationObserver(function () {
      if (insert()) observer.disconnect();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(function () { observer.disconnect(); }, 15000);
  }
})();

// Pulse the avatar of the last assistant message while it has no text content
(function thinkingIndicator() {
  new MutationObserver(function () {
    // Find all images that could be assistant avatars
    var avatars = document.querySelectorAll("img");
    avatars.forEach(function (img) {
      // Skip non-avatar images (header logo, etc)
      if (img.classList.contains("orionbelt-header-logo")) return;
      if (img.width > 40 || img.height > 40) return;

      // Check if this avatar's sibling/parent message area has empty content
      var msgContainer = img.closest("[class]");
      if (!msgContainer) return;

      // Walk up a few levels to find the message wrapper
      var wrapper = msgContainer;
      for (var i = 0; i < 5; i++) {
        if (!wrapper.parentElement) break;
        wrapper = wrapper.parentElement;
      }

      // Check if this message block has meaningful text
      var textContent = wrapper.textContent.trim();
      // Remove the avatar alt text from consideration
      var alt = img.alt || "";
      textContent = textContent.replace(alt, "").trim();

      if (textContent === "") {
        img.classList.add("orionbelt-thinking");
      } else {
        img.classList.remove("orionbelt-thinking");
      }
    });
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
})();

// Escape key → click the stop button to cancel generation
(function escapeToStop() {
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    // Chainlit renders the stop button with id="stop-button" only while generating
    var stopBtn = document.getElementById("stop-button");
    if (stopBtn) {
      e.preventDefault();
      e.stopPropagation();
      stopBtn.click();
    }
  }, true);  // capture phase — fire before React
})();

// Render ```mermaid code blocks as diagrams via Mermaid.js CDN
(function mermaidRenderer() {
  var script = document.createElement("script");
  script.src = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js";
  script.onload = function () {
    var isDark = document.documentElement.classList.contains("dark");
    mermaid.initialize({ startOnLoad: false, theme: isDark ? "dark" : "default" });

    // Re-initialize theme when dark mode toggles
    new MutationObserver(function () {
      var dark = document.documentElement.classList.contains("dark");
      mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default" });
      document.querySelectorAll(".orionbelt-mermaid-rendered").forEach(function (el) {
        delete el.dataset.mermaidRendered;
        el.removeAttribute("data-processed");
        el.innerHTML = el.dataset.mermaidSource || el.textContent;
      });
      renderMermaidBlocks();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

    function findMermaidCodeBlocks() {
      var results = [];
      document.querySelectorAll("pre").forEach(function (pre) {
        if (pre.dataset.mermaidProcessed) return;
        var code = pre.querySelector("code");
        if (!code) return;

        var isMermaid = false;

        // Check 1: code element has language-mermaid class
        if (/language-mermaid/i.test(code.className)) {
          isMermaid = true;
        }

        // Check 2: walk up from <pre> and look for "mermaid" label text
        // anywhere in the ancestor tree (up to 5 levels)
        if (!isMermaid) {
          var el = pre.parentElement;
          for (var depth = 0; depth < 5 && el; depth++) {
            // Search all descendant text nodes of this ancestor
            var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            var textNode;
            while ((textNode = walker.nextNode())) {
              // Skip text inside the <pre> itself
              if (pre.contains(textNode)) continue;
              if (textNode.textContent.trim().toLowerCase() === "mermaid") {
                isMermaid = true;
                break;
              }
            }
            if (isMermaid) break;
            el = el.parentElement;
          }
        }

        if (isMermaid) {
          pre.dataset.mermaidProcessed = "true";
          results.push({ code: code, pre: pre });
        }
      });
      return results;
    }

    function renderMermaidBlocks() {
      var blocks = findMermaidCodeBlocks();
      if (!blocks.length) return;

      blocks.forEach(function (block) {
        if (block.code.dataset.mermaidRendered) return;
        block.code.dataset.mermaidRendered = "true";

        var source = block.code.textContent;
        if (!source || !source.trim()) return;

        var container = document.createElement("div");
        container.className = "mermaid orionbelt-mermaid-rendered";
        container.dataset.mermaidSource = source;
        container.textContent = source;

        // Walk up from <pre> to find the outermost code block container.
        // Stop when the parent has other message content (multiple children
        // that aren't part of the code block chrome).
        var target = block.pre;
        var el = block.pre.parentElement;
        for (var i = 0; i < 4 && el; i++) {
          // If this ancestor ONLY contains code-block-related stuff
          // (the pre, labels, buttons) it's part of the wrapper
          var hasPre = el.querySelector("pre") === block.pre;
          var childCount = el.children.length;
          if (hasPre && childCount <= 4) {
            target = el;
          } else {
            break;
          }
          el = el.parentElement;
        }
        try {
          target.parentElement.replaceChild(container, target);
        } catch (_) { /* DOM may have been mutated by another observer */ }
      });

      try {
        mermaid.run({ querySelector: ".mermaid:not([data-processed])" });
      } catch (_) { /* ignore if no elements */ }
    }

    // Watch for new code blocks as messages stream in
    new MutationObserver(function () { renderMermaidBlocks(); })
      .observe(document.body, { childList: true, subtree: true });

    // Initial pass
    renderMermaidBlocks();
  };
  document.head.appendChild(script);
})();

// Resize Plotly chart containers — override Chainlit inline styles
(function plotlyResize() {
  function resize() {
    document.querySelectorAll(".js-plotly-plot").forEach(function (plot) {
      var container = plot.closest("[style]");
      if (container) {
        container.style.maxWidth = "100%";
        container.style.height = "auto";
      }
    });
  }
  new MutationObserver(resize)
    .observe(document.body, { childList: true, subtree: true });
})();

// Arrow Up in empty input → recall last user message (shell-style history)
(function arrowUpRecall() {
  var messageHistory = [];
  var historyIndex = -1;
  var savedInput = "";  // stash current input when entering history mode

  // Find the Chainlit chat input (id="chat-input", renders as <textarea>)
  function getInput() {
    return document.getElementById("chat-input");
  }

  function pushMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    // Deduplicate consecutive identical messages
    if (messageHistory.length && messageHistory[messageHistory.length - 1] === text) return;
    messageHistory.push(text);
    historyIndex = -1;
    savedInput = "";
  }

  // Set textarea value via execCommand so React's controlled component
  // picks up the change through normal browser input handling.
  function setInputValue(el, text) {
    el.focus();
    // Select all existing text, then replace with new text
    el.select();
    // execCommand('insertText') triggers native beforeinput+input events
    // that React handles identically to real user typing
    if (!document.execCommand("insertText", false, text)) {
      // Fallback: native setter + _valueTracker reset
      var nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, "value"
      ).set;
      var tracker = el._valueTracker;
      if (tracker) tracker.setValue(el.value);
      nativeSetter.call(el, text);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    // Place cursor at end
    el.selectionStart = el.selectionEnd = text.length;
  }

  // Capture user message right before Enter submits (capture phase fires before React)
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      var el = getInput();
      if (el) pushMessage(el.value);
    }
  }, true);

  // Also capture when the send button (id="chat-submit") is clicked
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("#chat-submit");
    if (btn) {
      var el = getInput();
      if (el) pushMessage(el.value);
    }
  }, true);

  // Arrow Up / Arrow Down to navigate message history (capture phase)
  document.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    var el = getInput();
    if (!el || document.activeElement !== el) return;
    if (messageHistory.length === 0) return;

    if (e.key === "ArrowUp") {
      // Only start recall when the input is empty or already in recall mode
      if (el.value.trim() !== "" && historyIndex === -1) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (historyIndex === -1) {
        // Save whatever is currently typed so Arrow Down can restore it
        savedInput = el.value;
        historyIndex = messageHistory.length - 1;
      } else if (historyIndex > 0) {
        historyIndex--;
      }
      setInputValue(el, messageHistory[historyIndex]);
    } else if (e.key === "ArrowDown" && historyIndex !== -1) {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (historyIndex < messageHistory.length - 1) {
        historyIndex++;
        setInputValue(el, messageHistory[historyIndex]);
      } else {
        // Past the newest entry → restore whatever the user had typed
        historyIndex = -1;
        setInputValue(el, savedInput);
        savedInput = "";
      }
    }
  }, true);
})();

// EU AI Act Art. 50(2): mark model-authored output in the DOM so the rendered
// page is machine-inspectable. This is a supplement, not the primary marking —
// it does not survive copy-paste, so payloads that leave the app are marked
// in-band instead (src/provenance.py).
(function markGeneratedContent() {
  var SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia";

  function tagDocument() {
    if (document.querySelector('meta[name="ai-generated"]')) return;
    var flag = document.createElement("meta");
    flag.name = "ai-generated";
    flag.content = "true";
    document.head.appendChild(flag);

    var source = document.createElement("meta");
    source.name = "digital-source-type";
    source.content = SOURCE_TYPE;
    document.head.appendChild(source);
  }

  // Chainlit tags every rendered step with its type; everything that is not a
  // user message is produced by the model or its tools.
  function tagMessages() {
    document.querySelectorAll("[data-step-type]").forEach(function (el) {
      if (el.getAttribute("data-step-type") === "user_message") return;
      if (el.dataset.aiGenerated === "true") return;
      el.dataset.aiGenerated = "true";
      el.dataset.digitalSourceType = SOURCE_TYPE;
    });
  }

  tagDocument();
  tagMessages();
  new MutationObserver(tagMessages).observe(document.body, {
    childList: true,
    subtree: true,
  });
})();

// Element side panel: opened only on request, and closed by the same pill.
//
// Chainlit opens it on its own — an effect watching the message elements sets
// the side view whenever any element has display "side", with the *last*
// element's title over *all* of them. For the MCP tool lists that means a
// session would open on one server's name above every server's tools run
// together, which is not a view of anything.
//
// Closing it is not enough on its own: a close can only run once the panel has
// mounted, so that state gets painted first. The `data-ob-side-panel`
// attribute set here is what header.css keys on to keep the panel out of the
// layout until it is genuinely wanted; this closes it as well, so Chainlit's
// own state matches what is on screen.
(function elementSidePanel() {
  var root = document.documentElement;
  var openedFor = null; // label of the pill the user opened, if any

  function panel() {
    return document.getElementById("side-view-title");
  }

  function markRequested(requested) {
    if (requested) root.setAttribute("data-ob-side-panel", "open");
    else root.removeAttribute("data-ob-side-panel");
  }

  function closePanel() {
    var open = panel();
    var button = open && open.querySelector("button");
    if (!button) return false;
    button.click();
    return true;
  }

  // Dismissed on every appearance, not once: the elements arrive as separate
  // socket events, so the effect that opens the panel runs once per server and
  // would reopen it behind a one-shot close. `openedFor` is what stops this
  // from fighting a panel the user opened on purpose.
  //
  // The panel is also mounted a frame or two before its close button exists,
  // so a single attempt on the mutation that revealed it is not enough.
  function dismissAutoOpen(attempt) {
    if (openedFor || !panel()) return;
    if (closePanel()) return;
    if (attempt < 60) {
      requestAnimationFrame(function () {
        dismissAutoOpen(attempt + 1);
      });
    }
  }

  document.addEventListener(
    "click",
    function (event) {
      var pill = event.target && event.target.closest && event.target.closest("a.element-link");
      if (!pill) return;
      var label = pill.textContent.trim();
      if (panel() && openedFor === label) {
        // Same pill twice: close what it opened instead of reopening it.
        event.preventDefault();
        event.stopPropagation();
        openedFor = null;
        markRequested(false);
        closePanel();
        return;
      }
      openedFor = label;
      markRequested(true);
    },
    true
  );

  dismissAutoOpen(0);
  new MutationObserver(function () {
    if (!panel()) {
      // Gone — dismissed by us, or closed with the panel's own button.
      if (openedFor) {
        openedFor = null;
        markRequested(false);
      }
      return;
    }
    dismissAutoOpen(0);
  }).observe(document.body, { childList: true, subtree: true });
})();
