/** Stands for "no value" in numbers sent to the GPU (NaN does not survive shaders reliably).
 * Gene values are decoded with it directly, so they go to the GPU without a copy. */
export const MISSING_VALUE = Math.fround(-3.0e38);

export const isMissing = (v: number) => !(v > -1e38) || !Number.isFinite(v);
