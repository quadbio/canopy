import { OrthographicView } from '@deck.gl/core';
import { ScatterplotLayer } from '@deck.gl/layers';
import DeckGL from '@deck.gl/react';
import { useEffect, useState } from 'react';

import { getHealth, type Health } from '../api/client';

// S0 smoke screen: backend health + one deck.gl canvas with two views, proving the
// rendering stack builds and runs. Replaced by the real shell in S4.
const points = Array.from({ length: 2000 }, (_, i) => ({
  position: [Math.cos(i) * (i % 97), Math.sin(i) * (i % 89)] as [number, number],
  group: i % 2,
}));

const views = [
  new OrthographicView({ id: 'left', x: 0, width: '50%' }),
  new OrthographicView({ id: 'right', x: '50%', width: '50%' }),
];

export function App() {
  const [health, setHealth] = useState<Health | string>('…');

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((e: Error) => setHealth(e.message));
  }, []);

  const layers = [
    new ScatterplotLayer({
      id: 'pts',
      data: points,
      getPosition: (d) => d.position,
      getFillColor: (d) => (d.group ? [214, 96, 77] : [67, 147, 195]),
      getRadius: 1.5,
    }),
  ];

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', fontFamily: 'system-ui' }}>
      <header style={{ padding: '8px 16px', borderBottom: '1px solid #ddd' }}>
        dbx · backend: {typeof health === 'string' ? health : `${health.status} (v${health.version})`}
      </header>
      <div style={{ position: 'relative', flex: 1 }}>
        <DeckGL
          views={views}
          initialViewState={{
            left: { target: [0, 0, 0], zoom: 1 },
            right: { target: [0, 0, 0], zoom: 2 },
          }}
          controller
          layers={layers}
        />
      </div>
    </div>
  );
}
