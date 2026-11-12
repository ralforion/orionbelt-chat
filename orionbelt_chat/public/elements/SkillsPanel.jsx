// Skills checklist: which Agent Skills the assistant may use in this chat.
//
// Rendered under the greeting by `_skill_elements` in orionbelt_chat/app.py.
// `props`: {skills: [{name, description, source}], disabled: [name], cookie, action}
//   disabled — the skills the server has switched off for this chat, among those listed
//   cookie   — where this browser keeps its choice, so it carries into the next chat
//   action   — the action callback that applies a choice to the running agent
//
// The browser's choice is the cookie. It also keeps names of skills not listed
// right now (a server that is down, a folder moved away), so they are still
// off when they come back.
//
// Only Tailwind classes already present in Chainlit's compiled CSS work here;
// anything else is an inline style.

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { useEffect, useState } from "react";

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

export default function SkillsPanel() {
  const listed = props.skills.map((s) => s.name);
  const [disabled, setDisabled] = useState(() => new Set(props.disabled));
  const [error, setError] = useState(null);

  const apply = (names) =>
    callAction({ name: props.action, payload: { disabled: names } })
      .then(() => setError(null))
      .catch(() => setError("Could not apply the change — it takes effect in the next chat."));

  // The server reads the cookie when a chat starts. A new chat that reuses the
  // connection it read it from would start on an older choice, so the panel
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

  const choose = (next) => {
    setDisabled(next);
    const unlisted = (readChoice(props.cookie) || []).filter((n) => !listed.includes(n));
    const names = [...unlisted, ...next];
    writeChoice(props.cookie, names);
    apply(names);
  };

  const toggle = (name, enabled) => {
    const next = new Set(disabled);
    if (enabled) next.delete(name);
    else next.add(name);
    choose(next);
  };

  const enabledCount = listed.filter((n) => !disabled.has(n)).length;

  return (
    <div
      className="flex flex-col gap-2 rounded-md border p-3 w-full bg-background text-foreground"
      style={{ maxWidth: "44rem" }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm">
          <span className="font-semibold">Skills</span>
          <span className="text-muted-foreground">
            {" "}· {enabledCount} of {listed.length} on — the assistant loads one when a task calls for it
          </span>
        </div>
        <div className="flex gap-1">
          <Button type="button" variant="ghost" size="sm" onClick={() => choose(new Set())}>
            All
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => choose(new Set(listed))}>
            None
          </Button>
        </div>
      </div>
      <div style={{ maxHeight: "16rem", overflowY: "auto" }}>
        {props.skills.map((skill, i) => (
          <label
            key={skill.name}
            className="flex items-start gap-3 py-2 cursor-pointer"
            style={i ? { borderTop: "1px solid hsl(var(--border))" } : undefined}
          >
            <Checkbox
              checked={!disabled.has(skill.name)}
              onCheckedChange={(value) => toggle(skill.name, value === true)}
              style={{ marginTop: "2px" }}
            />
            <div className="flex flex-col gap-1" style={{ minWidth: 0 }}>
              <div className="flex items-center gap-2 flex-wrap">
                <code className="text-sm font-medium">{skill.name}</code>
                <Badge variant="outline">{skill.source}</Badge>
              </div>
              <div
                className="text-xs text-muted-foreground"
                title={skill.description}
                style={{
                  display: "-webkit-box",
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical",
                  overflow: "hidden",
                }}
              >
                {skill.description}
              </div>
            </div>
          </label>
        ))}
      </div>
      {error ? (
        <div className="text-xs" style={{ color: "hsl(var(--destructive))" }}>
          {error}
        </div>
      ) : null}
    </div>
  );
}
