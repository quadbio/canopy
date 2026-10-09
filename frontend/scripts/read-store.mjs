// Read a dbx store over HTTP the way the browser will (Viv + zarrita), and print a summary.
// Usage: node scripts/read-store.mjs http://127.0.0.1:8777/c0
// Serve a store with e.g. `python -m http.server 8777` from its parent folder.
import { loadOmeZarr } from '@hms-dbmi/viv';
import * as zarr from 'zarrita';

const base = process.argv[2];
if (!base) throw new Error('usage: node scripts/read-store.mjs <store-url>');
const manifest = await (await fetch(`${base}/manifest.json`)).json();
const max = (a) => a.reduce((m, v) => (v > m ? v : m), a[0]);
const root = zarr.root(new zarr.FetchStore(base));
const arr = async (path, sel) => zarr.get(await zarr.open(root.resolve(path), { kind: 'array' }), sel);

for (const [name, img] of Object.entries(manifest.images)) {
  const { data } = await loadOmeZarr(`${base}/${img.path}`, { type: 'multiscales' });
  const tile = await data[0].getTile({ x: 0, y: 0, selection: { c: 0 } });
  console.log(`image ${name}: ${data.length} levels, ${data[0].dtype}, tile ${tile.width}x${tile.height}, max ${max(tile.data)}`);
}
for (const [name, lab] of Object.entries(manifest.labels)) {
  const { data } = await loadOmeZarr(`${base}/${lab.path}`, { type: 'multiscales' });
  const tile = await data[0].getTile({ x: 0, y: 0, selection: {} });
  console.log(`labels ${name}: ${data.length} levels, ${data[0].dtype}, max ${max(tile.data)}`);
  if (lab.lookup) console.log(`  lookup length ${(await arr(lab.lookup)).data.length}`);
}
for (const [name, sh] of Object.entries(manifest.shapes)) {
  const starts = await arr(`${sh.path}/${sh.kind === 'polygons' ? 'starts' : 'row'}`);
  console.log(`shapes ${name}: ${sh.kind}, ${starts.data.length} entries`);
}
for (const [name, pt] of Object.entries(manifest.points)) {
  const g = await zarr.open(root.resolve(pt.path), { kind: 'group' });
  const offsets = await arr(`${pt.path}/offsets`);
  const xy = await arr(`${pt.path}/xy`, [zarr.slice(0, 2), null]);
  console.log(`points ${name}: ${g.attrs.dbx.features.length} features, offsets[1]=${offsets.data[1]}, first xy ${Array.from(xy.data)}`);
}
for (const [name, t] of Object.entries(manifest.tables)) {
  const indptr = await arr(`${t.path}/X/indptr`, [zarr.slice(0, 3)]);
  console.log(`table ${name}: indptr head ${Array.from(indptr.data)}`);
}
