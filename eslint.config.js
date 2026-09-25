/**
 * ESLint for the packaged Chrome extension. The extension lives outside
 * web/src (it ships from src/byoai/browser_extension), so this is its lint
 * entry; run from the repo root with web/node_modules/.bin/eslint.
 * Minimal by design: the parts that matter are the ones that bite in a
 * content-script/service-worker context — undefined references, evaluated
 * code, deprecated patterns.
 */
export default [
  {
    files: ['src/byoai/browser_extension/**/*.js'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'script',
      globals: {
        chrome: 'readonly',
        window: 'readonly',
        document: 'readonly',
        location: 'readonly',
        crypto: 'readonly',
        navigator: 'readonly',
        // Web standard globals the extension legitimately uses everywhere.
        fetch: 'readonly',
        setTimeout: 'readonly',
        clearTimeout: 'readonly',
        CustomEvent: 'readonly',
        Date: 'readonly',
        JSON: 'readonly',
        Promise: 'readonly',
        String: 'readonly',
        Number: 'readonly',
        URL: 'readonly',
        console: 'readonly',
      },
    },
    rules: {
      'no-undef': 'error',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', caughtErrors: 'none' }],
      'no-eval': 'error',
      'prefer-const': 'warn',
    },
  },
]
