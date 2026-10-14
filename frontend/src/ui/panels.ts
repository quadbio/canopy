/** Asks the app shell to show the settings panel (when folded away), e.g. from a view's gear. */
export const SHOW_SETTINGS_EVENT = 'canopy:show-settings';

export function showSettings() {
  window.dispatchEvent(new Event(SHOW_SETTINGS_EVENT));
}
