// Run with: node scripts/test_map_provider_loader.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const loaderSource = fs.readFileSync(
    path.join(__dirname, '..', 'home', 'static', 'home', 'map-provider-loader.js'),
    'utf8'
);

function createBrowser({online = true, onlineResult = 'success'} = {}) {
    const requests = [];
    const browser = {
        MAP_PROVIDER_CONFIG: {
            onlineUrl: 'https://example.test/amap.js',
            offlineScriptUrl: '/maps/assets/offline-amap.js',
            onlineTimeoutMs: 50,
        },
        navigator: {onLine: online},
        console: {warn() {}},
        setTimeout,
        clearTimeout,
    };
    browser.window = browser;
    browser.document = {
        createElement() {
            return {dataset: {}, remove() {}};
        },
        head: {
            appendChild(script) {
                requests.push(script.dataset.mapProvider);
                queueMicrotask(() => {
                    if (script.dataset.mapProvider === 'online') {
                        if (onlineResult === 'failure') return script.onerror();
                        if (onlineResult === 'timeout') return;
                        browser.AMap = {Map() {}};
                        return script.onload();
                    }
                    browser.OfflineAMap = {
                        __offlineProvider: true,
                        Map() {},
                    };
                    browser.AMap = browser.OfflineAMap;
                    return script.onload();
                });
            },
        },
    };
    vm.runInNewContext(loaderSource, browser);
    return {browser, requests};
}

(async () => {
    const online = createBrowser();
    assert.equal(await online.browser.MapProviderLoader.load(), 'online');
    assert.deepEqual(online.requests, ['online']);

    const offline = createBrowser({online: false});
    assert.equal(await offline.browser.MapProviderLoader.load(), 'offline');
    assert.deepEqual(offline.requests, ['offline']);

    const fallback = createBrowser({onlineResult: 'failure'});
    assert.equal(await fallback.browser.MapProviderLoader.load(), 'offline');
    assert.deepEqual(fallback.requests, ['online', 'offline']);
    assert.equal(fallback.browser.AMap, fallback.browser.OfflineAMap);

    const timeoutFallback = createBrowser({onlineResult: 'timeout'});
    assert.equal(await timeoutFallback.browser.MapProviderLoader.load(), 'offline');
    assert.deepEqual(timeoutFallback.requests, ['online', 'offline']);

    console.log('Map provider loader regression passed.');
})().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
