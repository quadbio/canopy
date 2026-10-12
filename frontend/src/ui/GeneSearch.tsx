// Gene search: type part of a gene symbol or id, pick from the matches (mouse or arrow keys +
// Enter). Matching runs over the whole gene list in the browser (30–60 k names: < 5 ms).

import { useEffect, useId, useMemo, useRef, useState } from 'react';

import type { GeneList } from '../api/types';

const MAX_SHOWN = 50;

export interface GeneMatch {
  index: number;
  label: string; // symbol (or name)
  name: string; // var index name
}

// Lower-cased names and symbols, computed once per gene list.
const lowered = new WeakMap<GeneList, { names: string[]; symbols: string[] | null; labels: string[] }>();

function lower(genes: GeneList) {
  let l = lowered.get(genes);
  if (!l) {
    l = {
      names: genes.names.map((n) => n.toLowerCase()),
      symbols: genes.symbols?.map((x) => x.toLowerCase()) ?? null,
      labels: genes.names.map((n, i) => genes.symbols?.[i] || n),
    };
    lowered.set(genes, l);
  }
  return l;
}

/** Best matches first: exact, then prefix, then substring (shorter, then alphabetical, within
 * each); symbols and ids both searched. Only the best `limit` are sorted. */
export function matchGenes(genes: GeneList, query: string, limit = MAX_SHOWN): GeneMatch[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const l = lower(genes);
  const buckets: number[][] = [[], [], []];
  for (let i = 0; i < l.names.length; i += 1) {
    const sym = l.symbols?.[i] ?? '';
    const name = l.names[i];
    if (sym === q || name === q) buckets[0].push(i);
    else if (sym.startsWith(q) || name.startsWith(q)) buckets[1].push(i);
    else if (buckets[0].length + buckets[1].length < limit && (sym.includes(q) || name.includes(q))) buckets[2].push(i);
  }
  const byLabel = (a: number, b: number) => {
    const x = l.labels[a];
    const y = l.labels[b];
    return x.length - y.length || (x < y ? -1 : x > y ? 1 : 0);
  };
  const out: number[] = [];
  for (const b of buckets) {
    if (out.length >= limit) break;
    out.push(...b.sort(byLabel).slice(0, limit - out.length));
  }
  return out.map((i) => ({ index: i, label: l.labels[i], name: genes.names[i] }));
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
  const listRef = useRef<HTMLUListElement>(null);
  useEffect(() => {
    // keep the option chosen with the arrow keys in view
    listRef.current?.querySelector(`[data-k="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active]);
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
        onBlur={() => setOpen(false)}
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
        <ul
          className="gene-matches"
          id={listId}
          role="listbox"
          aria-label="Matching genes"
          ref={listRef}
          // the input keeps focus while a match is clicked (no timing guess on a slow desktop)
          onMouseDown={(e) => e.preventDefault()}
        >
          {matches.length === 0 && (
            <li className="muted small pad" role="presentation">
              <span role="status">No gene matches.</span>
            </li>
          )}
          {matches.map((m, k) => (
            <li
              key={m.index}
              id={`${listId}-${k}`}
              data-k={k}
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
          {matches.length === MAX_SHOWN && (
            <li className="muted small pad" role="presentation">
              Type more to narrow down.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
