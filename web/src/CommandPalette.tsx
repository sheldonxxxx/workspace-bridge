import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";

export type PaletteCommand = {
  id: string;
  label: string;
  group: string;
  keys?: string;
  run: () => void;
};

function matches(command: PaletteCommand, query: string): boolean {
  const haystack = `${command.group} ${command.label}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((part) => haystack.includes(part));
}

export function CommandPalette({
  open,
  onOpenChange,
  commands,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  commands: PaletteCommand[];
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const filtered = useMemo(
    () => commands.filter((command) => matches(command, query)),
    [commands, query],
  );
  const current = Math.min(active, Math.max(filtered.length - 1, 0));

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${current}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [current]);

  function change(next: boolean) {
    if (!next) {
      setQuery("");
      setActive(0);
    }
    onOpenChange(next);
  }

  function run(command: PaletteCommand | undefined) {
    if (!command) return;
    change(false);
    command.run();
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || (event.ctrlKey && event.key === "n")) {
      event.preventDefault();
      setActive(filtered.length ? (current + 1) % filtered.length : 0);
    } else if (
      event.key === "ArrowUp" ||
      (event.ctrlKey && event.key === "p")
    ) {
      event.preventDefault();
      setActive(
        filtered.length ? (current - 1 + filtered.length) % filtered.length : 0,
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      run(filtered[current]);
    }
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent className="palette" showCloseButton={false}>
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <DialogDescription className="sr-only">
          Type to filter commands, use the arrow keys to choose, and press Enter
          to run.
        </DialogDescription>
        <label className="palette-input">
          <span aria-hidden="true">&gt;</span>
          <input
            autoFocus
            role="combobox"
            aria-expanded="true"
            aria-controls="palette-list"
            aria-activedescendant={
              filtered[current] ? `palette-${filtered[current].id}` : undefined
            }
            aria-label="Search commands"
            placeholder="type a command or section…"
            value={query}
            spellCheck={false}
            autoComplete="off"
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={onKeyDown}
          />
        </label>
        <div
          id="palette-list"
          className="palette-list"
          role="listbox"
          aria-label="Commands"
          ref={listRef}
        >
          {filtered.length === 0 && (
            <p className="palette-empty">
              <span aria-hidden="true">!</span> no command matches “{query}”
            </p>
          )}
          {filtered.map((command, index) => {
            const heading =
              index === 0 || filtered[index - 1].group !== command.group;
            return (
              <div key={command.id} role="presentation">
                {heading && (
                  <div className="palette-group" role="presentation">
                    {command.group}
                  </div>
                )}
                <div
                  id={`palette-${command.id}`}
                  data-index={index}
                  role="option"
                  aria-selected={index === current}
                  className="palette-item"
                  onMouseMove={() => setActive(index)}
                  onClick={() => run(command)}
                >
                  <span>{command.label}</span>
                  {command.keys && <kbd>{command.keys}</kbd>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="palette-foot" aria-hidden="true">
          <span>
            <kbd>↑</kbd>
            <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>↵</kbd> run
          </span>
          <span>
            <kbd>esc</kbd> close
          </span>
        </div>
      </DialogContent>
    </Dialog>
  );
}
