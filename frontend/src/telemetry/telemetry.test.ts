import { describe, expect, it } from 'vitest';

import { browser, caller, deckErrorFields, messageCode, where } from './telemetry';

describe('deck errors in telemetry', () => {
  it('keep the layer class and the message, never layer ids (they hold element names)', () => {
    const e = new Error("update of PathLayer(v1/main:cells_of_patient7-line): Cannot read properties of undefined (reading 'updateGeometry')");
    const f = deckErrorFields(e);
    expect(f.layer).toBe('PathLayer');
    expect(f.message).not.toContain('patient7');
    expect(f.message).toContain('Cannot read properties of undefined');
  });
});

describe('telemetry codes', () => {
  it('messages lose quoted parts, ids in parentheses and addresses', () => {
    const m = messageCode("Cannot read 'patient_7' of undefined (at http://host.inst:8765/x) see https://a.b/c");
    expect(m).not.toContain('patient_7');
    expect(m).not.toContain('host.inst');
    expect(m).toContain('Cannot read');
  });
  it('the error location is the bundle file, line and column', () => {
    const stack = 'TypeError: x\n    at f (http://127.0.0.1:8765/assets/index-AbC123.js:1:2345)\n    at g (x.js:2:3)';
    expect(where(stack)).toBe('index-AbC123.js:1:2345');
    expect(where(undefined)).toBe('unknown');
    expect(caller(stack)).toBe('x.js:2:3');
    expect(caller(undefined)).toBe('unknown');
  });
  it('browser family and major version', () => {
    expect(browser('Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML) Chrome/140.0.1 Safari/537.36')).toBe('Chrome 140');
    expect(browser('Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML) Version/18.2 Safari/605.1.15')).toBe('Safari 18');
    expect(browser('Mozilla/5.0 Gecko/20100101 Firefox/141.0')).toBe('Firefox 141');
  });
});
