import { describe, expect, it } from 'vitest';

import { memberTitle, sharedPrefix } from './titles';

describe('member titles (review full-2 C-M5)', () => {
  it('leave out the prefix all members share, at a separator', () => {
    const t = ['202302171205_region_1_scwf.zarr', '202302171205_region_3_scwf.zarr'];
    expect(sharedPrefix(t)).toBe('202302171205_region_');
    expect(memberTitle(t[0], sharedPrefix(t))).toBe('…1_scwf.zarr');
    expect(sharedPrefix(['sample_A.h5ad', 'sample_B.h5ad'])).toBe('sample_');
    expect(sharedPrefix(['a.h5ad'])).toBe('');
  });

  it('shorten a long rest in the middle, keeping its end', () => {
    const s = memberTitle('sample_with_a_very_long_name_that_ends_in_ID_0042.h5ad', '');
    expect(s.length).toBe(28);
    expect(s.endsWith('0042.h5ad')).toBe(true);
  });
});
