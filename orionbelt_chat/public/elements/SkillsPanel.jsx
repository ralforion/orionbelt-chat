// Skills: which Agent Skills the assistant may use in this chat.
//
// Rendered under the greeting by `_skill_elements` in orionbelt_chat/app.py.
// `props`: {skills: [{name, description, source}], disabled: [name], cookie, action}
//   disabled — the skills the server has switched off for this chat, among those listed
//   cookie   — where this browser keeps its choice, so it carries into the next chat
//   action   — the action callback that applies a choice to the running agent
//
// This element owns the choice: only an element can call a Chainlit action.
// What the user ticks lives in a floating pane opened by the Skills button
// under the chat input, drawn by public/header.js so it stays in reach however
// long the chat gets. The two talk through window events:
//   orionbelt:skills-state   here → pane   {skills, disabled, error}, or null when gone
//   orionbelt:skills-open    here → pane   the Manage button below
//   orionbelt:skills-choose  pane → here   {disabled}
//
// The browser's choice is the cookie. It also keeps names of skills not listed
// right now (a server that is down, a folder moved away), so they are still
// off when they come back.

import { Button } from "@/components/ui/button";
import { useEffect, useRef, useState } from "react";

const STATE_EVENT = "orionbelt:skills-state";
const OPEN_EVENT = "orionbelt:skills-open";
const CHOOSE_EVENT = "orionbelt:skills-choose";
const ONE_YEAR = 60 * 60 * 24 * 365;

function readChoice(cookie) {
  const entry = document.cookie.split("; ").find((c) => c.startsWith(cookie + "="));
  if (!entry) return null;
  try {
    const names = JSON.parse(decodeURIComponent(entry.slice(cookie.length + 1)));
    return Array.isArray(names) ? names.filter((n) => typeof n === "string") : null;
  } catch {
    return null;
  }
}

function writeChoice(cookie, names) {
  const value = encodeURIComponent(JSON.stringify([...new Set(names)].sort()));
  document.cookie = `${cookie}=${value}; path=/; max-age=${ONE_YEAR}; SameSite=Lax`;
}

function sameNames(a, b) {
  const left = new Set(a);
  return left.size === new Set(b).size && [...b].every((n) => left.has(n));
}

function publish(detail) {
  // Kept on window too, for a pane script that starts listening late.
  window.__orionbeltSkills = detail;
  window.dispatchEvent(new CustomEvent(STATE_EVENT, { detail }));
}

export default function SkillsPanel() {
  const listed = props.skills.map((s) => s.name);
  const [disabled, setDisabled] = useState(() => new Set(props.disabled));
  const [error, setError] = useState(null);

  const apply = (names) =>
    callAction({ name: props.action, payload: { disabled: names } })
      .then(() => setError(null))
      .catch(() => setError("Could not apply the change — it takes effect in the next chat."));

  const choose = (next) => {
    const off = new Set([...next].filter((n) => listed.includes(n)));
    setDisabled(off);
    const unlisted = (readChoice(props.cookie) || []).filter((n) => !listed.includes(n));
    const names = [...unlisted, ...off];
    writeChoice(props.cookie, names);
    apply(names);
  };

  // The server reads the cookie when a chat starts. A new chat that reuses the
  // connection it read it from would start on an older choice, so the element
  // checks, and re-sends this browser's choice if the two disagree.
  useEffect(() => {
    const stored = readChoice(props.cookie);
    if (stored === null) return;
    const wanted = stored.filter((n) => listed.includes(n));
    if (!sameNames(wanted, props.disabled)) {
      setDisabled(new Set(wanted));
      apply(stored);
    }
  }, []);

  // Keep the pane current; withdraw it when this chat's greeting goes away.
  useEffect(() => {
    publish({ skills: props.skills, disabled: [...disabled], error });
  }, [disabled, error]);
  useEffect(() => () => publish(null), []);

  // Choices made in the pane. A ref, so the listener added once always calls
  // the latest `choose`.
  const chooseRef = useRef(choose);
  chooseRef.current = choose;
  useEffect(() => {
    const onChoose = (event) => chooseRef.current(new Set(event.detail?.disabled || []));
    window.addEventListener(CHOOSE_EVENT, onChoose);
    return () => window.removeEventListener(CHOOSE_EVENT, onChoose);
  }, []);

  const enabledCount = listed.filter((n) => !disabled.has(n)).length;

  return (
    <div
      className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 w-full bg-background text-foreground"
      style={{ maxWidth: "44rem" }}
    >
      <div className="text-sm">
        <span className="font-semibold">Skills</span>
        <span className="text-muted-foreground">
          {" "}· {enabledCount} of {listed.length} on — change them any time with Skills under the
          message box
        </span>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => window.dispatchEvent(new CustomEvent(OPEN_EVENT))}
      >
        Manage
      </Button>
      {error ? (
        <div className="text-xs" style={{ color: "hsl(var(--destructive))" }}>
          {error}
        </div>
      ) : null}
    </div>
  );
}
