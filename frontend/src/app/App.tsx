// Application shell: header, file browser (left), the canvas (centre), the selected view's
// settings (right).

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import { AUTH_REQUIRED_EVENT } from '../api/client';
import { catalog } from '../api/endpoints';
import { metaQuery, useCatalogStatus } from '../api/queries';
import { MAX_COLUMNS, useWorkspace, type ViewSpec } from '../state/workspace';
import { event } from '../telemetry/telemetry';
import { FileBrowser, sharedDefaults } from '../ui/FileBrowser';
import { CloseAll } from '../ui/CloseAll';
import { SHOW_SETTINGS_EVENT } from '../ui/panels';
import { useTabs } from '../state/tabs';
import { JobWatcher } from './JobWatcher';
import { Canvas } from '../views/Canvas';
import { viewType } from '../views/registry';

type Theme = 'system' | 'light' | 'dark';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem('canopy-theme') as Theme) || 'system';
    } catch {
      return 'system';
    }
  });
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    try {
      localStorage.setItem('canopy-theme', theme);
    } catch {
      // storage unavailable: the choice lasts for this page only
    }
  }, [theme]);
  const next: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' };
  return [theme, () => setTheme(next[theme])];
}

function CatalogStatus() {
  const status = useCatalogStatus();
  const qc = useQueryClient();
  const scanning = status.data?.scanning;
  const last = status.data?.last_scan;
  const seen = useRef<number | null | undefined>(undefined);
  useEffect(() => {
    // Reload the listings when a scan finishes, not when the status first arrives.
    if (scanning || last === undefined) return;
    if (seen.current !== undefined && seen.current !== (last?.id ?? null)) {
      void qc.invalidateQueries({ predicate: (q) => ['roots', 'folder', 'search'].includes(String(q.queryKey[0])) });
    }
    seen.current = last?.id ?? null;
  }, [scanning, last, qc]);
  const when = last?.finished ? new Date(last.finished * 1000).toLocaleTimeString() : null;
  return (
    <div className="catalog-status">
      <span className="muted small">{scanning ? 'Scanning…' : when ? `Indexed ${when}` : ''}</span>
      <button className="ghost" onClick={() => void catalog.refresh().then(() => status.refetch())} disabled={scanning}>
        Refresh
      </button>
    </div>
  );
}

/** Another tab of this browser saves the workspace: this one's changes would overwrite it, so
 * they are not saved until the user says so (review full-2 C-H2). */
function OtherTabNote() {
  const saving = useTabs((s) => s.saving);
  const takeOver = useTabs((s) => s.takeOver);
  if (saving) return null;
  return (
    <div className="other-tab-note" role="status" data-testid="other-tab-note">
      Canopy is open in another tab of this browser, which saves the workspace: changes here are not kept after
      a reload.{' '}
      <button className="link" onClick={takeOver} data-testid="save-from-this-tab">
        Save from this tab instead
      </button>
    </div>
  );
}

function SettingsPanel() {
  const spec = useWorkspace((s) => s.views.find((v) => v.id === s.selected) ?? null);
  const members = useWorkspace((s) => (spec?.group ? s.views.filter((v) => v.group === spec.group).length : 0));
  const t = spec ? viewType(spec.type) : undefined;
  return (
    <aside className="panel" aria-label="View settings">
      {spec && t ? (
        <>
          {/* a group's settings are the group's, not one member's (review full-2 C-H1) */}
          <h3 className="panel-title" title={spec.group ? undefined : spec.title} data-testid="panel-title">
            {spec.group ? `Linked · ${members} datasets` : spec.title}
          </h3>
          <p className="muted small">
            {t.label}
            {spec.group ? ` · own choices shown for ${spec.title}` : ''}
          </p>
          {spec.group && <LinkedNote spec={spec} />}
          <t.Panel key={spec.id} spec={spec} />
          <ResetView key={`reset-${spec.id}`} spec={spec} />
        </>
      ) : (
        <p className="muted small pad">Select a view to change its settings.</p>
      )}
    </aside>
  );
}

/** In a linked group: what the settings apply to. */
function LinkedNote({ spec }: { spec: ViewSpec }) {
  const n = useWorkspace((s) => s.views.filter((v) => v.group === spec.group).length);
  const own = viewType(spec.type)?.ownKeys ?? [];
  return (
    <p className="small linked-note" data-testid="linked-note">
      Linked with {n - 1} other dataset{n === 2 ? '' : 's'}: changes apply to all of them
      {own.includes('embedding') ? ', except the embedding (chosen per dataset)' : ''}.
    </p>
  );
}

/** Back to the view type's defaults for this dataset (the remembered settings are replaced too). */
function ResetView({ spec }: { spec: ViewSpec }) {
  const qc = useQueryClient();
  const setParams = useWorkspace((s) => s.setParams);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const reset = async () => {
    const t = viewType(spec.type);
    if (!t) return;
    setBusy(true);
    setFailed(null);
    try {
      const meta = await qc.fetchQuery(metaQuery(spec.datasetId, spec.table));
      let params = t.create(meta) as Record<string, unknown> | null;
      if (params && spec.group) {
        // a linked group: the shared defaults of all its datasets, as when it was opened
        // (this dataset's own keys from its defaults; the others keep theirs, B-L9)
        const members = useWorkspace.getState().views.filter((v) => v.group === spec.group);
        const metas = await Promise.all(members.map((v) => qc.fetchQuery(metaQuery(v.datasetId, v.table))));
        const own = t.ownKeys ?? [];
        params = { ...sharedDefaults(metas, params), ...Object.fromEntries(own.filter((k) => k in params!).map((k) => [k, params![k]])) };
      }
      if (params) {
        setParams(spec.id, params);
        event('view_reset', null, { type: spec.type });
      }
    } catch (e) {
      setFailed(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="panel-footer">
      <button className="ghost" onClick={() => void reset()} disabled={busy} data-testid="reset-view">
        Reset to defaults
      </button>
      {failed && <p className="error small">{failed}</p>}
    </div>
  );
}

function ResetWorkspace() {
  return <CloseAll title="Close all views and start empty" />;
}

/** Side panels (datasets, settings) shown or folded away, remembered in this browser; folded
 * by default on narrow windows. */
function useSidePanels() {
  const [shown, setShown] = useState<{ datasets: boolean; settings: boolean }>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('canopy-panels') ?? 'null') as { datasets?: boolean; settings?: boolean } | null;
      if (saved) return { datasets: saved.datasets !== false, settings: saved.settings !== false };
    } catch {
      // ignore
    }
    return { datasets: true, settings: window.innerWidth >= 1200 };
  });
  const set = (k: 'datasets' | 'settings', on: (was: boolean) => boolean) =>
    setShown((s) => {
      if (on(s[k]) === s[k]) return s;
      const next = { ...s, [k]: on(s[k]) };
      try {
        localStorage.setItem('canopy-panels', JSON.stringify(next));
      } catch {
        // private window: not remembered
      }
      return next;
    });
  const toggle = (k: 'datasets' | 'settings') => set(k, (was) => !was);
  // a view's gear shows the settings
  useEffect(() => {
    const on = () => set('settings', () => true);
    window.addEventListener(SHOW_SETTINGS_EVENT, on);
    return () => window.removeEventListener(SHOW_SETTINGS_EVENT, on);
  }, []);
  return [shown, toggle] as const;
}

/** The server refused a request for lack of the access token (e.g. the cookie was cleared). */
function useAuthRequired() {
  const [needed, setNeeded] = useState(false);
  useEffect(() => {
    const on = () => setNeeded(true);
    window.addEventListener(AUTH_REQUIRED_EVENT, on);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, on);
  }, []);
  return needed;
}

/** The number of columns of the grid of views (issue #1): rows stay rows as views open and
 * close (e.g. one dataset per row). */
function ColumnsChoice() {
  const n = useWorkspace((s) => s.views.length);
  const columns = useWorkspace((s) => s.columns);
  const setColumns = useWorkspace((s) => s.setColumns);
  // also with one view while a number is set, so it can always be set back
  if (n < 2 && columns === null) return null;
  return (
    <label className="columns-choice" title="Columns of the grid of views; Auto: as square as possible">
      Columns
      <select
        value={columns ?? 'auto'}
        onChange={(e) => setColumns(e.target.value === 'auto' ? null : Number(e.target.value))}
        data-testid="columns-select"
      >
        <option value="auto">Auto</option>
        {Array.from({ length: MAX_COLUMNS }, (_, i) => (
          <option key={i + 1} value={i + 1}>
            {i + 1}
          </option>
        ))}
      </select>
    </label>
  );
}

export function App() {
  const [theme, cycleTheme] = useTheme();
  const [panels, togglePanel] = useSidePanels();
  const authRequired = useAuthRequired();
  if (authRequired) {
    return (
      <div className="auth-required" role="alert" data-testid="auth-required">
        <img src="/canopy-128.png" alt="" width={64} height={64} />
        <h1>Canopy needs its access link</h1>
        <p>
          Open the link with <code>?token=…</code> that was printed when the server started. On the server,{' '}
          <code>canopy link --config &lt;config&gt;</code> prints it again.
        </p>
      </div>
    );
  }
  return (
    <div className={`app${panels.datasets ? '' : ' no-sidebar'}${panels.settings ? '' : ' no-panel'}`}>
      <header className="header">
        <img className="brand-logo" src="/canopy-64.png" srcSet="/canopy-64.png 1x, /canopy-128.png 2x" alt="" width={28} height={28} />
        <span className="brand">Canopy</span>
        <span className="muted small">single-cell and spatial data browser</span>
        <div className="spacer" />
        <button
          className="ghost"
          onClick={() => togglePanel('datasets')}
          aria-pressed={panels.datasets}
          title={panels.datasets ? 'Fold the datasets panel away' : 'Show the datasets panel'}
          data-testid="toggle-datasets"
        >
          Datasets
        </button>
        <button
          className="ghost"
          onClick={() => togglePanel('settings')}
          aria-pressed={panels.settings}
          title={panels.settings ? 'Fold the settings panel away' : 'Show the settings panel'}
          data-testid="toggle-settings"
        >
          Settings
        </button>
        <CatalogStatus />
        <ColumnsChoice />
        <ResetWorkspace />
        <button className="ghost" onClick={cycleTheme} title="Theme: system, light or dark">
          Theme: {theme === 'system' ? 'auto' : theme}
        </button>
      </header>
      <JobWatcher />
      <FileBrowser />
      <main className="main">
        <OtherTabNote />
        <Canvas />
      </main>
      <SettingsPanel />
    </div>
  );
}
