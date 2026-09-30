"use client";

import { useMemo, useState } from "react";

import { Kbd } from "@/components/ui/controls";

export interface Command {
  id: string;
  label: string;
  group: string;
  shortcut?: string;
  run: () => void;
}

/** Ctrl/Cmd+K palette: type to filter, arrows to move, Enter to run, Esc to close. */
/** Mounted only while open, so every opening starts with an empty query. */
export function CommandPalette({ onClose, commands }: { onClose: () => void; commands: Command[] }) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? commands.filter((c) => `${c.group} ${c.label}`.toLowerCase().includes(q)) : commands;
  }, [commands, query]);

  const runAt = (i: number) => {
    const cmd = filtered[i];
    if (cmd) {
      onClose();
      cmd.run();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-[12vh]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-[min(560px,92vw)] overflow-hidden rounded-md border border-line-strong bg-panel shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <input
          autoFocus
          value={query}
          onChange={(e) => { setQuery(e.target.value); setIndex(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setIndex((i) => Math.min(i + 1, filtered.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setIndex((i) => Math.max(i - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); runAt(index); }
            else if (e.key === "Escape") { e.preventDefault(); onClose(); }
          }}
          placeholder="Type a command…"
          aria-label="Search commands"
          aria-controls="palette-list"
          aria-activedescendant={filtered[index] ? `cmd-${filtered[index].id}` : undefined}
          className="w-full border-b border-line bg-transparent px-4 py-3 text-[14px] text-fg outline-none placeholder:text-muted"
        />
        <ul id="palette-list" role="listbox" className="max-h-[50vh] overflow-y-auto py-1">
          {filtered.length === 0 && <li className="px-4 py-2 text-muted">No matching commands</li>}
          {filtered.map((c, i) => (
            <li
              key={c.id}
              id={`cmd-${c.id}`}
              role="option"
              aria-selected={i === index}
              onMouseEnter={() => setIndex(i)}
              onClick={() => runAt(i)}
              className={`flex cursor-pointer items-center justify-between px-4 py-1.5 ${i === index ? "bg-raised text-fg" : "text-fg-2"}`}
            >
              <span><span className="text-muted">{c.group} · </span>{c.label}</span>
              {c.shortcut && <Kbd>{c.shortcut}</Kbd>}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

