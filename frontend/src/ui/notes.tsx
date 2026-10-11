// Dataset notes in plain language. The inspector reports structural facts as codes
// (backend/dbx/inspect/core.py `_warnings`); here each code says what it means for the user and
// whether anything needs doing.

export interface Notice {
  code: string;
  message: string;
  kind?: string | null;
  element?: string | null;
}

interface Explained {
  title: string;
  /** What it means for the user; `names` lists the affected parts of the dataset, if any. */
  text: (names: string) => string;
  /** `problem`: something may not work; `info`: handled by dbx, nothing to do. */
  level: 'info' | 'problem';
}

const EXPLAIN: Record<string, Explained> = {
  x_csr: {
    title: 'Expression is stored cell by cell',
    text: (n) =>
      `The expression matrix${n ? ` of ${n}` : ''} is stored one cell after another (CSR). Colouring by a gene reads one gene ` +
      'across all cells, so dbx loads the matrix into memory once (fast for files up to a few GB). Larger files need a ' +
      'one-time conversion, which is not available in this version yet.',
    level: 'info',
  },
  x_dtype: {
    title: 'Expression values are whole numbers',
    text: (n) =>
      `The expression matrix${n ? ` of ${n}` : ''} holds integers, usually raw counts rather than normalised values. ` +
      'Shown as they are.',
    level: 'info',
  },
  legacy_layout: {
    title: 'Written by an old AnnData version',
    text: (n) =>
      `${n ? `${n} uses` : 'This file uses'} an older file layout. Small files open normally (read into memory); ` +
      'large ones will need a one-time conversion.',
    level: 'info',
  },
  shape_mismatch: {
    title: 'Matrix size does not match the cell and gene lists',
    text: (n) =>
      `In ${n || 'this dataset'} the expression matrix has a different size than the lists of cells and genes. ` +
      'The file may be damaged or incompletely written; expression values may not be usable.',
    level: 'problem',
  },
  no_spatialdata_attrs: {
    title: 'No SpatialData version recorded',
    text: () => 'The dataset does not say which SpatialData version wrote it. dbx reads it as the current format; unusual files may fail.',
    level: 'info',
  },
  single_level: {
    title: 'Image without zoom levels',
    text: (n) =>
      `${n ? `Image ${n} has` : 'An image has'} only its full resolution (no smaller copies for zoomed-out views). ` +
      'When images are shown, dbx makes a one-time copy with zoom levels so they stay fast.',
    level: 'info',
  },
  single_chunk: {
    title: 'Image stored as one block',
    text: (n) =>
      `${n ? `Image ${n} is` : 'An image is'} stored as a single block, so any part of it means reading all of it. ` +
      'Handled by the same one-time copy.',
    level: 'info',
  },
  no_feature_key: {
    title: 'Transcripts have no gene column',
    text: (n) =>
      `The points${n ? ` in ${n}` : ''} do not say which column holds the gene name, so transcripts cannot yet be coloured or filtered by gene.`,
    level: 'info',
  },
  element_errors: {
    title: 'Some parts could not be read',
    text: () => 'The rest of the dataset works. The server log names the parts and the reasons.',
    level: 'problem',
  },
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

export function DatasetNotes({ notices }: { notices: Notice[] }) {
  if (!notices.length) return null;
  // One entry per code, listing the parts it applies to.
  const byCode = new Map<string, Notice[]>();
  for (const n of notices) byCode.set(n.code, [...(byCode.get(n.code) ?? []), n]);
  const items = [...byCode.entries()].map(([code, list]) => {
    const names = list.map((n) => n.element).filter((e): e is string => !!e);
    const ex = EXPLAIN[code];
    return {
      code,
      title: ex?.title ?? code,
      text: ex ? ex.text(names.join(', ')) : list[0].message,
      level: ex?.level ?? 'info',
    };
  });
  const problems = items.filter((i) => i.level === 'problem').length;
  return (
    <details className="small notes" data-testid="dataset-notes">
      <summary>
        {problems
          ? `${plural(problems, 'possible problem')}${items.length > problems ? `, ${plural(items.length - problems, 'note')}` : ''}`
          : `${plural(items.length, 'note')} on how the data is stored (nothing to do)`}
      </summary>
      <ul>
        {items.map((i) => (
          <li key={i.code} className={i.level === 'problem' ? 'error' : undefined}>
            <strong>{i.title}.</strong> {i.text}
          </li>
        ))}
      </ul>
    </details>
  );
}
