// Colouring on the GPU. Points carry one float each (a category code or a number); a small
// RGBA lookup table (a texture) turns it into a colour in the vertex shader. Changing the
// palette, the colour map, the range or the hidden categories changes only the table or a
// uniform: no point data is sent again. Hidden categories (alpha 0 in the table) are moved out
// of view, so they are neither drawn nor picked.
//
// Usage: add `new ValueColourExtension()` to a layer, give it `data.attributes.getColourValue`
// (Float32Array, size 1; see `gpuValues`) and a `valueColour` prop.

import { type Layer, LayerExtension, type UpdateParameters } from '@deck.gl/core';
import type { Texture } from '@luma.gl/core';
import type { ShaderModule } from '@luma.gl/shadertools';

/** Stands for "no value" in the numbers sent to the GPU (NaN does not survive shaders reliably). */
export const MISSING_VALUE = Math.fround(-3.0e38);

export interface ValueColour {
  /** RGBA entries, 4 bytes each: categories by code, or a colour map (256 entries). */
  lut: Uint8Array;
  mode: 'categories' | 'numbers';
  /** Numbers: the values mapped to the first and the last entry. */
  range?: [number, number];
  /** RGBA 0–255 for missing values (code < 0, or MISSING_VALUE). */
  missing: [number, number, number, number];
}

const LUT_WIDTH = 256;

type Props = { valueColour: ValueColour | null };

type Uniforms = {
  range: [number, number];
  missing: [number, number, number, number];
  lutSize: number;
  numbers: number;
  enabled: number;
  valueColour_lut?: Texture;
};

const vs = /* glsl */ `
in float colourValues;
uniform sampler2D valueColour_lut;
layout(std140) uniform valueColourUniforms {
  vec4 missing;
  vec2 range;
  float lutSize;
  float numbers;
  float enabled;
} valueColour;

vec4 valueColour_rgba = vec4(0.0);

vec4 valueColour_lookup(float v) {
  float idx;
  if (valueColour.numbers > 0.5) {
    if (v < -1.0e38) return valueColour.missing;
    float span = valueColour.range.y - valueColour.range.x;
    float t = span > 0.0 ? clamp((v - valueColour.range.x) / span, 0.0, 1.0) : 0.5;
    idx = floor(t * (valueColour.lutSize - 1.0) + 0.5);
  } else {
    if (v < -0.5 || v > valueColour.lutSize - 0.5) return valueColour.missing;
    idx = floor(v + 0.5);
  }
  ivec2 p = ivec2(int(mod(idx, ${LUT_WIDTH}.0)), int(floor(idx / ${LUT_WIDTH}.0)));
  return texelFetch(valueColour_lut, p, 0);
}
`;

const inject = {
  'vs:#main-start': /* glsl */ `
  if (valueColour.enabled > 0.5) valueColour_rgba = valueColour_lookup(colourValues);
`,
  'vs:DECKGL_FILTER_GL_POSITION': /* glsl */ `
  if (valueColour.enabled > 0.5 && valueColour_rgba.a == 0.0) position = vec4(0.0, 0.0, 2.0, 1.0);
`,
  'vs:DECKGL_FILTER_COLOR': /* glsl */ `
  if (valueColour.enabled > 0.5) color = vec4(valueColour_rgba.rgb, color.a * valueColour_rgba.a);
`,
};

const valueColourModule = {
  name: 'valueColour',
  vs,
  inject,
  getUniforms: (opts?: Partial<Uniforms>) => opts ?? {},
  // vec4 first: std140 pads a vec4 that follows a vec2, keep the layout trivially aligned.
  uniformTypes: {
    missing: 'vec4<f32>',
    range: 'vec2<f32>',
    lutSize: 'f32',
    numbers: 'f32',
    enabled: 'f32',
  },
} as unknown as ShaderModule<Uniforms>;

type State = { valueColourTexture?: Texture; valueColourLut?: Uint8Array };

export class ValueColourExtension extends LayerExtension {
  static extensionName = 'ValueColourExtension';
  static defaultProps = {
    getColourValue: { type: 'accessor', value: 0 },
    valueColour: null,
  };

  getShaders() {
    return { modules: [valueColourModule] };
  }

  initializeState(this: Layer<Props>) {
    this.getAttributeManager()?.add({
      // 'dynamic': one value per instance (point) in instanced layers, per vertex otherwise.
      colourValues: { size: 1, stepMode: 'dynamic', accessor: 'getColourValue', defaultValue: 0 },
    });
  }

  updateState(this: Layer<Props>, { props }: UpdateParameters<Layer<Props>>) {
    const state = this.state as State;
    const lut = props.valueColour?.lut;
    if (!lut || lut === state.valueColourLut) return;
    const entries = Math.max(1, lut.length / 4);
    const height = Math.ceil(entries / LUT_WIDTH);
    const padded = new Uint8Array(LUT_WIDTH * height * 4);
    padded.set(lut);
    state.valueColourTexture?.destroy();
    state.valueColourTexture = this.context.device.createTexture({
      format: 'rgba8unorm',
      width: LUT_WIDTH,
      height,
      data: padded,
      sampler: { minFilter: 'nearest', magFilter: 'nearest', addressModeU: 'clamp-to-edge', addressModeV: 'clamp-to-edge' },
    });
    state.valueColourLut = lut;
  }

  draw(this: Layer<Props>) {
    const vc = this.props.valueColour;
    const state = this.state as State;
    const uniforms: Uniforms =
      vc && state.valueColourTexture
        ? {
            range: vc.range ?? [0, 1],
            missing: vc.missing.map((v) => v / 255) as Uniforms['missing'],
            lutSize: vc.lut.length / 4,
            numbers: vc.mode === 'numbers' ? 1 : 0,
            enabled: 1,
            valueColour_lut: state.valueColourTexture,
          }
        : { range: [0, 1], missing: [0, 0, 0, 0], lutSize: 1, numbers: 0, enabled: 0, valueColour_lut: dummy(this) };
    this.setShaderModuleProps({ valueColour: uniforms });
  }

  finalizeState(this: Layer<Props>) {
    (this.state as State).valueColourTexture?.destroy();
  }
}

// A 1×1 texture so the sampler is always bound, even before the first table arrives.
const dummies = new WeakMap<object, Texture>();
function dummy(layer: Layer): Texture {
  const device = layer.context.device;
  let t = dummies.get(device);
  if (!t) {
    t = device.createTexture({ format: 'rgba8unorm', width: 1, height: 1, data: new Uint8Array(4) });
    dummies.set(device, t);
  }
  return t;
}

/** Values as the GPU takes them: Float32, missing values as MISSING_VALUE (codes stay as they
 * are; negative codes mean missing). Done once per loaded column. */
export function gpuValues(values: ArrayLike<number>, numbers: boolean): Float32Array {
  const out = new Float32Array(values.length);
  if (numbers) {
    for (let i = 0; i < values.length; i += 1) {
      const v = values[i];
      out[i] = Number.isFinite(v) ? v : MISSING_VALUE;
    }
  } else out.set(values);
  return out;
}
