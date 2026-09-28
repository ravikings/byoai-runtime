/**
 * The consent version, in one place for every part of the extension that
 * checks it: the service worker (importScripts), the relay (listed before it
 * in the manifest) and the popup (a <script> before popup.js).
 *
 * Version 2: the extension also replaces personal details in the page. That
 * is a different thing to agree to, so an agreement to version 1 doesn't count.
 */
globalThis.SHIELD_CONSENT_VERSION = 2
