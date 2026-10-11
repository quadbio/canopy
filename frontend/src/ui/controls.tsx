// Small form controls on Radix primitives (keyboard and screen-reader support built in),
// styled only through design tokens (styles/app.css).

import { Popover as RPopover, Select as RSelect, Slider as RSlider } from 'radix-ui';
import type { ReactNode } from 'react';

export interface Option {
  value: string;
  label: string;
  hint?: string;
}

export interface OptionGroup {
  label: string;
  options: Option[];
}

const NONE = '__none__';

export function Select({
  value,
  onChange,
  groups,
  placeholder = 'Choose…',
  allowNone,
  label,
  testId,
}: {
  value: string | null;
  onChange: (v: string | null) => void;
  groups: OptionGroup[];
  placeholder?: string;
  allowNone?: string;
  label: string;
  testId?: string;
}) {
  return (
    <RSelect.Root value={value ?? (allowNone ? NONE : undefined)} onValueChange={(v) => onChange(v === NONE ? null : v)}>
      <RSelect.Trigger className="select-trigger" aria-label={label} data-testid={testId}>
        <RSelect.Value placeholder={placeholder} />
        <RSelect.Icon className="select-icon">▾</RSelect.Icon>
      </RSelect.Trigger>
      <RSelect.Portal>
        <RSelect.Content className="select-content" position="popper" sideOffset={4}>
          <RSelect.Viewport>
            {allowNone && (
              <RSelect.Item value={NONE} className="select-item">
                <RSelect.ItemText>{allowNone}</RSelect.ItemText>
              </RSelect.Item>
            )}
            {groups.map((g) => (
              <RSelect.Group key={g.label}>
                {groups.length > 1 && <RSelect.Label className="select-label">{g.label}</RSelect.Label>}
                {g.options.map((o) => (
                  <RSelect.Item key={o.value} value={o.value} className="select-item">
                    <RSelect.ItemText>{o.label}</RSelect.ItemText>
                    {o.hint && <span className="select-hint">{o.hint}</span>}
                  </RSelect.Item>
                ))}
              </RSelect.Group>
            ))}
          </RSelect.Viewport>
        </RSelect.Content>
      </RSelect.Portal>
    </RSelect.Root>
  );
}

export function Slider({
  value,
  onChange,
  min,
  max,
  step,
  label,
  format = (v) => String(v),
}: {
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  format?: (v: number) => string;
}) {
  return (
    <div className="slider-row">
      <RSlider.Root
        className="slider"
        value={[value]}
        min={min}
        max={max}
        step={step}
        onValueChange={([v]) => onChange(v)}
        aria-label={label}
      >
        <RSlider.Track className="slider-track">
          <RSlider.Range className="slider-range" />
        </RSlider.Track>
        <RSlider.Thumb className="slider-thumb" aria-label={label} />
      </RSlider.Root>
      <span className="slider-value">{format(value)}</span>
    </div>
  );
}

/** A slider with two thumbs, for a range (e.g. an image channel's contrast). */
export function RangeSlider({
  value,
  onChange,
  min,
  max,
  step,
  label,
  format = (v) => String(v),
}: {
  value: [number, number];
  onChange: (v: [number, number]) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  format?: (v: number) => string;
}) {
  return (
    <div className="slider-row">
      <RSlider.Root
        className="slider"
        value={value}
        min={min}
        max={max}
        step={step}
        minStepsBetweenThumbs={1}
        onValueChange={([lo, hi]) => onChange([lo, hi])}
        aria-label={label}
      >
        <RSlider.Track className="slider-track">
          <RSlider.Range className="slider-range" />
        </RSlider.Track>
        <RSlider.Thumb className="slider-thumb" aria-label={`${label}: lowest`} />
        <RSlider.Thumb className="slider-thumb" aria-label={`${label}: highest`} />
      </RSlider.Root>
      <span className="slider-value">
        {format(value[0])}–{format(value[1])}
      </span>
    </div>
  );
}

/** A small "?" that explains a term: opens on click or tap (Escape or a click outside closes it). */
export function Help({ children, label = 'What is this?' }: { children: ReactNode; label?: string }) {
  return (
    <RPopover.Root>
      <RPopover.Trigger asChild>
        <button type="button" className="help" aria-label={label}>
          ?
        </button>
      </RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content className="help-content" side="top" align="start" sideOffset={6} collisionPadding={8}>
          {children}
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );
}

export function Field({ label, help, children }: { label: string; help?: ReactNode; children: ReactNode }) {
  // With a help button inside, a <label> would forward its clicks to the control: use a <div>.
  const Tag = help ? 'div' : 'label';
  return (
    <Tag className="field">
      <span className="field-label">
        {label}
        {help && <Help label={`About “${label}”`}>{help}</Help>}
      </span>
      {children}
    </Tag>
  );
}

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–';
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)} k`;
  return String(n);
}

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return '–';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
