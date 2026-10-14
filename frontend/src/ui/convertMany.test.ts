import { QueryClient } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client';

const convert = vi.fn();
vi.mock('../api/endpoints', () => ({ jobs: { convert: (id: string) => convert(id) } }));
vi.mock('../telemetry/telemetry', () => ({ event: () => undefined }));

const { RETRY_MS, useConvertMany } = await import('./convertMany');

describe('convert ticked', () => {
  afterEach(() => vi.useRealTimers());

  it('hands the datasets over in order, waiting while the server’s queue is full', async () => {
    vi.useFakeTimers();
    let full = true;
    convert.mockImplementation(async (id: string) => {
      if (id === 'c' && full) throw new ApiError(429, '/x', null, { code: 'queue_full', message: 'full' });
      if (id === 'bad') throw new ApiError(409, '/x', null, { code: 'state:failed', message: 'cannot' });
      return { id: 1 };
    });
    useConvertMany.getState().add(['a', 'bad', 'c', 'd'], new QueryClient());
    await vi.waitFor(() => expect(useConvertMany.getState().waiting).toEqual(['c', 'd']));
    expect(useConvertMany.getState().failed).toEqual([{ id: 'bad', message: 'cannot' }]);
    full = false;
    await vi.advanceTimersByTimeAsync(RETRY_MS);
    await vi.waitFor(() => expect(useConvertMany.getState().waiting).toEqual([]));
    expect(convert.mock.calls.map((c) => c[0])).toEqual(['a', 'bad', 'c', 'c', 'd']);
  });
});
