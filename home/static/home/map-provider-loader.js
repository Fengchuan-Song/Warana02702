/* Select the online AMap API first and install the local AMap facade on failure. */
(function (global) {
    'use strict';

    const config = Object.assign({
        onlineUrl: '',
        securityJsCode: '',
        onlineTimeoutMs: 10000,
        offlineScriptUrl: '/maps/assets/offline-amap.js',
    }, global.MAP_PROVIDER_CONFIG || {});

    let loadPromise = null;
    let offlinePromise = null;

    function loadScript(url, timeoutMs, provider) {
        return new Promise((resolve, reject) => {
            const script = global.document.createElement('script');
            let settled = false;
            const timeoutId = global.setTimeout(() => {
                if (settled) return;
                settled = true;
                script.remove();
                reject(new Error(`${provider}地图脚本加载超时`));
            }, timeoutMs);

            script.async = true;
            script.src = url;
            script.dataset.mapProvider = provider;
            script.onload = () => {
                if (settled) {
                    // A timed-out cross-origin script may still finish after its
                    // element is removed. Keep the already selected local facade.
                    if (provider === 'online' && global.OfflineAMap) {
                        global.AMap = global.OfflineAMap;
                    }
                    return;
                }
                settled = true;
                global.clearTimeout(timeoutId);
                resolve();
            };
            script.onerror = () => {
                if (settled) return;
                settled = true;
                global.clearTimeout(timeoutId);
                script.remove();
                reject(new Error(`${provider}地图脚本加载失败`));
            };
            global.document.head.appendChild(script);
        });
    }

    function hasMapApi() {
        return Boolean(global.AMap && typeof global.AMap.Map === 'function');
    }

    function removeOnlineApi() {
        try {
            delete global.AMap;
        } catch (error) {
            global.AMap = undefined;
        }
    }

    function useOffline() {
        if (global.OfflineAMap) {
            global.AMap = global.OfflineAMap;
            return Promise.resolve('offline');
        }
        if (global.AMap && global.AMap.__offlineProvider) {
            return Promise.resolve('offline');
        }
        if (offlinePromise) return offlinePromise;

        removeOnlineApi();
        offlinePromise = loadScript(
            config.offlineScriptUrl,
            Math.max(Number(config.onlineTimeoutMs) || 10000, 3000),
            'offline'
        ).then(() => {
            if (!hasMapApi()) throw new Error('离线地图接口未正确注册');
            return 'offline';
        });
        return offlinePromise;
    }

    async function loadPreferredProvider() {
        if (global.navigator && global.navigator.onLine === false) {
            return useOffline();
        }
        if (!config.onlineUrl) return useOffline();

        if (config.securityJsCode) {
            global._AMapSecurityConfig = {
                securityJsCode: config.securityJsCode,
            };
        }

        try {
            await loadScript(
                config.onlineUrl,
                Number(config.onlineTimeoutMs) || 10000,
                'online'
            );
            if (!hasMapApi()) throw new Error('高德在线地图接口未正确注册');
            return 'online';
        } catch (error) {
            global.console.warn('高德在线地图不可用，将使用本地离线地图。', error);
            return useOffline();
        }
    }

    global.MapProviderLoader = Object.freeze({
        load() {
            if (!loadPromise) loadPromise = loadPreferredProvider();
            return loadPromise;
        },
        useOffline,
    });
})(window);
