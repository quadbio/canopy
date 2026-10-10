// Registers every view type: each `views/<type>/index.tsx` calls `registerView` when imported.
// Adding a view type means adding its folder; nothing else changes.
import.meta.glob('./*/index.tsx', { eager: true });
