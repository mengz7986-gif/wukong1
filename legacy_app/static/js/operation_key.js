/* Ordinary HTTP omits randomUUID, but still supports cryptographic randomness.
   Preserve native UUID implementations and never fall back to Math.random. */
(function () {
    'use strict';
    var api = globalThis.crypto;
    if (!api || typeof api.randomUUID === 'function' || typeof api.getRandomValues !== 'function') return;
    api.randomUUID = function () {
        var bytes = new Uint8Array(16);
        api.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        var hex = Array.from(bytes, function (value) { return value.toString(16).padStart(2, '0'); }).join('');
        return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
    };
})();
