// "Close all": empties the canvas. With a linked group or several views open it asks first, in
// place (one click would discard a 20-view group; review H3 B-L4). The dataset details stay.

import { useState } from 'react';

import { useWorkspace } from '../state/workspace';

export function CloseAll({ title }: { title?: string }) {
  const reset = useWorkspace((s) => s.reset);
  const n = useWorkspace((s) => s.views.length);
  const group = useWorkspace((s) => s.views.some((v) => v.group));
  const [asking, setAsking] = useState(false);
  if (asking && n) {
    return (
      <span className="close-all-ask" role="group" aria-label="Close all views?">
        <button
          className="ghost danger"
          onClick={() => {
            reset();
            setAsking(false);
          }}
          data-testid="close-all-confirm"
          autoFocus
        >
          Close {group ? `the ${n} linked views` : `${n} views`}
        </button>
        <button className="link small" onClick={() => setAsking(false)}>
          Keep
        </button>
      </span>
    );
  }
  return (
    <button className="ghost" onClick={() => (group || n > 1 ? setAsking(true) : reset())} disabled={!n} title={title}>
      Close all
    </button>
  );
}
