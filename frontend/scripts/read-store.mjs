// Read a dbx store over HTTP the way the browser will (Viv + zarrita) and assert the contract.
// Usage: node scripts/read-store.mjs <store-dir>     serve the folder with a minimal Range server
//        node scripts/read-store.mjs <store-url>     read from a running `dbx serve`
//                                                    (http://host:port/api/stores/<version>/c0)
// Counts requests and bytes (by wrapping fetch) and exits non-zero on any mismatch.
import assert from 'node:assert/strict';
import { createReadStream, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { join, normalize } from 'node:path';
import { loadOmeZarr } from '@hms-dbmi/viv';
import * as zarr from 'zarrita';

const target = process.argv[2];
if (!target) throw new Error('usage: node scripts/read-store.mjs <store-dir | store-url>');

const stats = { requests: 0, ranged: 0, bytes: 0, notFound: [], head: 0 };
const realFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const req = new Request(input, init);
  const res = await realFetch(req);
  stats.requests += 1;
  if (req.method === 'HEAD') stats.head += 1;
  if (req.headers.get('range')) stats.ranged += 1;
  if (res.status === 404) stats.notFound.push(new URL(req.url).pathname.slice(storePath.length));
  if (res.status === 206 && !res.headers.get('content-range')) throw new Error(`206 without content-range: ${req.url}`);
  stats.bytes += Number(res.headers.get('content-length') ?? 0);
  return res;
};

const dir = target.startsWith('http') ? null : target;
const server = dir && createServer((req, res) => {
  const path = normalize(join(dir, decodeURIComponent(new URL(req.url, 'http://x').pathname)));
  let size;
  try {
    size = statSync(path).size;
  } catch {
    res.writeHead(404).end();
    return;
  }
  const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range ?? '');
  let start = 0;
  let end = size - 1;
  if (m) {
    if (m[1] === '') start = Math.max(0, size - Number(m[2])); // suffix range: last N bytes
    else {
      start = Number(m[1]);
      if (m[2] !== '') end = Math.min(Number(m[2]), size - 1); // RFC 9110: clamp to the file
    }
    res.writeHead(206, { 'content-range': `bytes ${start}-${end}/${size}`, 'content-length': end - start + 1 });
  } else {
    res.writeHead(200, { 'content-length': size });
  }
  createReadStream(path, { start, end }).pipe(res);
});
if (server) await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = server ? `http://127.0.0.1:${server.address().port}` : target.replace(/\/$/, '');
const storePath = new URL(base).pathname.replace(/\/$/, '');

try {
  const manifest = await (await fetch(`${base}/manifest.json`)).json();
  const max = (a) => a.reduce((acc, v) => (v > acc ? v : acc), a[0]);
  // useSuffixRequest: read shard indexes with one `bytes=-N` request. The default path (HEAD, then
  // a range) is buggy in @zarrita/storage 0.1.4: it asks for `length` bytes past the index start.
  const root = zarr.root(new zarr.FetchStore(base, { useSuffixRequest: true }));
  const open = (path) => zarr.open.v3(root.resolve(path), { kind: 'array' });
  const get = async (path, sel) => zarr.get(await open(path), sel);

  for (const [name, img] of Object.entries(manifest.images)) {
    const { data } = await loadOmeZarr(`${base}/${img.path}`, { type: 'multiscales' });
    assert.equal(data.length, img.levels.length, `image ${name}: levels`);
    const tile = await data[0].getTile({ x: 0, y: 0, selection: { c: 0 } });
    assert.equal(tile.width, Math.min(512, img.levels[0][2]));
    console.log(`image ${name}: ${data.length} levels, ${data[0].dtype}, tile max ${max(tile.data)}`);
  }
  for (const [name, lab] of Object.entries(manifest.labels)) {
    const { data } = await loadOmeZarr(`${base}/${lab.path}`, { type: 'multiscales' });
    const tile = await data[0].getTile({ x: 0, y: 0, selection: {} });
    assert.ok(tile.data instanceof Uint32Array, `labels ${name}: uint32`);
    assert.ok(max(tile.data) <= lab.max_label);
    if (lab.lookup) {
      const lk = await get(lab.lookup);
      assert.ok(lk.data instanceof Int32Array && lk.data.length === lab.max_label + 1);
    }
    console.log(`labels ${name}: ${data.length} levels, tile max ${max(tile.data)}`);
  }
  for (const [name, sh] of Object.entries(manifest.shapes)) {
    if (sh.kind !== 'polygons') continue;
    // Zoomed in: block boxes, then one block's starts, then its vertex range.
    const blocks = await get(`${sh.path}/block_bbox`);
    const nb = Math.min(4096, sh.n);
    const starts = await get(`${sh.path}/starts`, [zarr.slice(0, nb + 1)]);
    assert.ok(starts.data instanceof Uint32Array, `shapes ${name}: starts must be uint32`);
    const verts = await get(`${sh.path}/vertices`, [zarr.slice(starts.data[0], starts.data[nb]), null]);
    const v = verts.data;
    const s0 = starts.data[0];
    const s1 = starts.data[1] - s0;
    assert.deepEqual([v[0], v[1]], [v[2 * (s1 - 1)], v[2 * (s1 - 1) + 1]], 'ring is closed');
    let area = 0; // shoelace over the first ring
    for (let i = 0; i < s1 - 1; i += 1) area += v[2 * i] * v[2 * i + 3] - v[2 * i + 2] * v[2 * i + 1];
    assert.ok(area > 0, 'ring is counter-clockwise');
    // Zoomed out: centroids only.
    const cent = await get(`${sh.path}/centroid`);
    assert.equal(cent.data.length, 2 * sh.n);
    console.log(`shapes ${name}: ${sh.n} polygons in ${blocks.shape[0]} blocks, first block ${v.length / 2} vertices`);
  }
  for (const [name, pt] of Object.entries(manifest.points)) {
    const g = await zarr.open.v3(root.resolve(pt.path), { kind: 'group' });
    assert.equal(g.attrs.dbx.features.length, pt.n_features);
    const offsets = (await get(`${pt.path}/offsets`)).data;
    assert.ok(offsets instanceof Uint32Array, 'offsets must be uint32');
    // One gene: its range of the sharded xy array.
    const f = 1;
    const xy = await get(`${pt.path}/xy`, [zarr.slice(offsets[f], offsets[f + 1]), null]);
    assert.equal(xy.data.length, 2 * (offsets[f + 1] - offsets[f]));
    const bb = (await get(`${pt.path}/chunk_bbox`)).data;
    assert.ok(xy.data[0] >= bb[0] - 1e9); // readable as numbers
    // Zoomed out: the materialised overview in one go.
    const ov = await get(`${pt.path}/overview/xy`);
    assert.equal(ov.data.length, 2 * pt.n_overview);
    const lodOff = (await get(`${pt.path}/lod/offsets`)).data;
    assert.equal(lodOff.length, pt.n_features + 1);
    console.log(`points ${name}: gene ${f} has ${offsets[f + 1] - offsets[f]} points; overview ${pt.n_overview}; lod ${lodOff[pt.n_features]}`);
  }
  for (const [name, t] of Object.entries(manifest.tables)) {
    const indptr = await get(`${t.path}/X/indptr`, [zarr.slice(0, 3)]);
    const j = 0;
    const lo = Number(indptr.data[j]);
    const hi = Number(indptr.data[j + 1]);
    const vals = await get(`${t.path}/X/data`, [zarr.slice(lo, hi)]);
    const rows = await get(`${t.path}/X/indices`, [zarr.slice(lo, hi)]);
    assert.equal(vals.data.length, rows.data.length);
    for (const [key, dims] of Object.entries(t.embeddings)) {
      const e = await get(`${t.path}/obsm/${key}`, [zarr.slice(0, 2), null]);
      assert.equal(e.data.length, 2 * dims);
    }
    console.log(`table ${name}: gene 0 has ${hi - lo} cells; embeddings ${Object.keys(t.embeddings)}`);
  }
  // Viv's loadOmeZarr probes zarr v2 metadata first (2 extra requests per image); our own reads
  // open zarr v3 explicitly and must never ask for a missing file.
  const unexpected = stats.notFound.filter((u) => !/^\/(images|labels)\/[^/]+\/\.z(attrs|group)$/.test(u));
  assert.deepEqual(unexpected, [], 'requests for missing files');
  assert.equal(stats.head, 0, 'no HEAD requests (suffix requests are used)');
  console.log(`OK: ${stats.requests} requests (${stats.ranged} range, ${stats.notFound.length} Viv v2 probes), ${(stats.bytes / 1024).toFixed(0)} KB`);
} finally {
  server?.close();
}
