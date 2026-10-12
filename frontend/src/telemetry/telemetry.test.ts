import { describe, expect, it } from 'vitest';

import { deckErrorFields } from './telemetry';

describe('deck errors in telemetry', () => {
  it('keep the layer class and the message, never layer ids (they hold element names)', () => {
    const e = new Error("update of PathLayer(v1/main:cells_of_patient7-line): Cannot read properties of undefined (reading 'updateGeometry')");
    const f = deckErrorFields(e);
    expect(f.layer).toBe('PathLayer');
    expect(f.message).not.toContain('patient7');
    expect(f.message).toContain('Cannot read properties of undefined');
  });
});
