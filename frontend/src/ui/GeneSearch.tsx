// Gene search: type part of a gene symbol or id, pick from the matches (mouse or arrow keys +
// Enter). Matching runs over the whole gene list in the browser (30–60 k names: < 5 ms).

import { useId, useMemo, useState } from 'react';

import type { GeneList } from '../api/types';

const MAX_SHOWN = 50;

export interface GeneMatch {
  index: number;
  label: string; // symbol (or name)
  name: string; // var index name
}

/** Best matches first: exact, then prefix, then substring; symbols and ids both searched. */
export function matchGenes(genes: GeneList, query: string, limit = MAX_SHOWN): GeneMatch[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const ranked: [number, number][] = [];
  for (let i = 0; i < genes.names.length; i += 1) {
    const sym = genes.symbols?.[i]?.toLowerCase() ?? '';
    const name = genes.names[i].toLowerCase();
    let rank = -1;
    if (sym === q || name === q) rank = 0;
    else if (sym.startsWith(q) || name.startsWith(q)) rank = 1;
    else if (sym.includes(q) || name.includes(q)) rank = 2;
    if (rank >= 0) ranked.push([rank, i]);
  }
  const label = (i: number) => genes.symbols?.[i] || genes.names[i];
  ranked.sort((a, b) => a[0] - b[0] || label(a[1]).length - label(b[1]).length || label(a[1]).localeCompare(label(b[1])));
  return ranked.slice(0, limit).map(([, i]) => ({ index: i, label: label(i), name: genes.names[i] }));
}

export function GeneSearch({
  genes,
  onPick,
  testId,
}: {
  genes: GeneList;
  onPick: (name: string) => void;
  testId?: string;
}) {
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const [open, setOpen] = useState(false);
  const listId = useId();
  const matches = useMemo(() => matchGenes(genes, query), [genes, query]);
  const pick = (m: GeneMatch) => {
    onPick(m.name);
    setQuery('');
    setOpen(false);
  };
  return (
    <div className="gene-search">
      <input
        type="search"
        placeholder={`Search ${genes.names.length.toLocaleString()} genes…`}
        value={query}
        role="combobox"
        aria-label="Search genes"
        aria-expanded={open && matches.length > 0}
        aria-controls={listId}
        aria-activedescendant={open && matches[active] ? `${listId}-${active}` : undefined}
        data-testid={testId}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)} // let a click on a match land first
        onKeyDown={(e) => {
          if (e.key === 'ArrowDown') setActive((a) => Math.min(a + 1, matches.length - 1));
          else if (e.key === 'ArrowUp') setActive((a) => Math.max(a - 1, 0));
          else if (e.key === 'Enter' && matches[active]) pick(matches[active]);
          else if (e.key === 'Escape') setOpen(false);
          else return;
          e.preventDefault();
        }}
      />
      {open && query.trim() && (
        <ul className="gene-matches" id={listId} role="listbox" aria-label="Matching genes">
          {matches.length === 0 && <li className="muted small pad">No gene matches.</li>}
          {matches.map((m, k) => (
            <li
              key={m.index}
              id={`${listId}-${k}`}
              role="option"
              aria-selected={k === active}
              className={`gene-match${k === active ? ' active' : ''}`}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pick(m)}
              onMouseEnter={() => setActive(k)}
            >
              <span>{m.label}</span>
              {m.label !== m.name && <span className="muted small">{m.name}</span>}
            </li>
          ))}
          {matches.length === MAX_SHOWN && <li className="muted small pad">Type more to narrow down.</li>}
        </ul>
      )}
    </div>
  );
}
