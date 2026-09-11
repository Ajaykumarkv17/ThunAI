/**
 * vite.config.ts — build config for the ThunAI frontend.
 *
 * Minimal setup: the React plugin (JSX + Fast Refresh) is all three surfaces
 * need. `import.meta.env.VITE_*` values are injected by Amplify Hosting at
 * build time (see infra/stacks/frontend_stack.py) and consumed in main.tsx.
 */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
});
