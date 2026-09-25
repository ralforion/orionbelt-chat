// MCP elicitation prompt: an MCP server asking the user for input.
//
// Rendered by `ask_user` in orionbelt_chat/app.py through AskElementMessage.
// `props` is a view built by orionbelt_chat/mcp_elicitation.py:
//   form mode: {mode: "form", server, message, fields, values, errors}
//   url mode:  {mode: "url", server, message, url, host, url_parts, warnings}
// Answers go back as submitElement({action, content}); cancelElement() is the
// spec's "cancel" (dismissed without an explicit choice).
//
// Only Tailwind classes already present in Chainlit's compiled CSS work here;
// anything else is an inline style.

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useState } from "react";

const INPUT_TYPES = { email: "email", uri: "url", date: "date", "date-time": "datetime-local" };

export default function McpElicitation() {
  return (
    <div className="flex flex-col gap-3 rounded-md border p-4 max-w-lg w-full bg-background text-foreground">
      <div className="text-xs text-muted-foreground">
        Requested by MCP server <span className="font-semibold text-foreground">{props.server}</span>
      </div>
      {/* Plain text on purpose: the spec says links in a server's message must not be clickable. */}
      <div className="text-sm whitespace-pre-wrap">{props.message}</div>
      {props.mode === "url" ? <UrlRequest /> : <FormRequest />}
    </div>
  );
}

// Explicit button types: inside the form, an untyped button is a submit
// button, so Cancel and Decline would otherwise submit the answers too.
// Without `onAccept` the accept button submits the enclosing form instead.
function Actions({ onAccept, acceptLabel }) {
  return (
    <div className="flex flex-wrap gap-2 justify-end">
      <Button type="button" variant="ghost" onClick={() => cancelElement()}>Cancel</Button>
      <Button type="button" variant="outline" onClick={() => submitElement({ action: "decline" })}>
        Decline
      </Button>
      <Button type={onAccept ? "button" : "submit"} onClick={onAccept}>{acceptLabel}</Button>
    </div>
  );
}

function UrlRequest() {
  // Spec: show the full URL before consent, highlight the domain, and open
  // it only on an explicit user action, in a context the client cannot read.
  const [before, host, after] = props.url_parts;
  const open = () => {
    window.open(props.url, "_blank", "noopener,noreferrer");
    submitElement({ action: "accept" });
  };
  return (
    <>
      <div className="flex flex-col gap-1">
        <div className="text-xs text-muted-foreground">This will open</div>
        <div className="text-sm font-semibold">{props.host}</div>
        <div className="rounded-md bg-muted p-3 font-mono text-xs" style={{ wordBreak: "break-all" }}>
          <span className="text-muted-foreground">{before}</span>
          <span className="font-semibold">{host}</span>
          <span className="text-muted-foreground">{after}</span>
        </div>
      </div>
      {props.warnings.map((w) => (
        <div key={w} className="text-sm text-destructive">⚠ {w}</div>
      ))}
      <div className="text-xs text-muted-foreground">
        Whatever you enter on that page goes to the server directly, not through this chat.
      </div>
      <Actions onAccept={open} acceptLabel="Open in new tab" />
    </>
  );
}

function FormRequest() {
  const [values, setValues] = useState(props.values || {});
  const set = (name, value) => setValues((v) => ({ ...v, [name]: value }));
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        submitElement({ action: "accept", content: values });
      }}
    >
      {Object.keys(props.errors).length > 0 ? (
        <div className="text-sm text-destructive">
          Some answers need fixing — nothing has been sent to the server yet.
        </div>
      ) : null}
      {props.fields.map((f) => (
        <Field key={f.name} field={f} value={values[f.name]} error={props.errors[f.name]} set={set} />
      ))}
      <div className="text-xs text-muted-foreground">
        Review your answers — nothing is sent until you submit.
      </div>
      <Actions acceptLabel="Submit" />
    </form>
  );
}

function Field({ field: f, value, error, set }) {
  const id = `mcp-elicit-${f.name}`;
  const label = (
    <Label htmlFor={id}>
      {f.title}
      {f.required ? <span className="text-destructive"> *</span> : null}
    </Label>
  );
  const hint = f.description ? <div className="text-xs text-muted-foreground">{f.description}</div> : null;
  const problem = error ? <div className="text-xs text-destructive">{error}</div> : null;

  if (f.kind === "boolean") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <Checkbox id={id} checked={!!value} onCheckedChange={(c) => set(f.name, c === true)} />
          {label}
        </div>
        {hint}
        {problem}
      </div>
    );
  }

  let control;
  if (f.kind === "enum") {
    control = (
      <select
        id={id}
        className={`h-9 w-full rounded-md border bg-background p-1 text-sm ${error ? "border-destructive" : ""}`}
        value={value ?? ""}
        onChange={(e) => set(f.name, e.target.value)}
      >
        <option value="">{f.required ? "Choose…" : "—"}</option>
        {f.options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    );
  } else if (f.kind === "multi_enum") {
    const picked = Array.isArray(value) ? value : [];
    const toggle = (v, on) => set(f.name, on ? [...picked, v] : picked.filter((p) => p !== v));
    control = (
      <div className="flex flex-col gap-1">
        {f.options.map((o) => (
          <div key={o.value} className="flex items-center gap-2">
            <Checkbox
              id={`${id}-${o.value}`}
              checked={picked.includes(o.value)}
              onCheckedChange={(c) => toggle(o.value, c === true)}
            />
            <Label htmlFor={`${id}-${o.value}`}>{o.label}</Label>
          </div>
        ))}
      </div>
    );
  } else {
    const numeric = f.kind === "number" || f.kind === "integer";
    control = (
      <Input
        id={id}
        className={error ? "border-destructive" : ""}
        type={numeric ? "number" : INPUT_TYPES[f.format] || "text"}
        step={f.kind === "integer" ? 1 : numeric ? "any" : undefined}
        min={f.minimum ?? undefined}
        max={f.maximum ?? undefined}
        minLength={f.minLength ?? undefined}
        maxLength={f.maxLength ?? undefined}
        value={value ?? ""}
        onChange={(e) => set(f.name, e.target.value)}
      />
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {label}
      {hint}
      {control}
      {problem}
    </div>
  );
}
