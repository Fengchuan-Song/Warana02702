/*
 * Small AMap-compatible facade backed by Leaflet 1.9.4.
 * It keeps the dashboard's existing overlay API while loading XYZ tiles only
 * from this Django application. No request is sent to an online map service.
 */
(function (global) {
    'use strict';

    if (!global.L) {
        throw new Error('Leaflet is required before offline-amap.js');
    }

    const L = global.L;
    const config = Object.assign({
        tileUrl: '/maps/tiles/{z}/{x}/{y}.png',
        minZoom: 1,
        maxZoom: 15,
    }, global.OFFLINE_MAP_CONFIG || {});
    const transparentTile =
        'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';

    class Pixel {
        constructor(x, y) {
            this.x = Number(x) || 0;
            this.y = Number(y) || 0;
        }
        getX() { return this.x; }
        getY() { return this.y; }
    }

    class Size {
        constructor(width, height) {
            this.width = Number(width) || 0;
            this.height = Number(height) || 0;
        }
        getWidth() { return this.width; }
        getHeight() { return this.height; }
    }

    class LngLat {
        constructor(lng, lat) {
            this.lng = Number(lng);
            this.lat = Number(lat);
        }
        getLng() { return this.lng; }
        getLat() { return this.lat; }
        toArray() { return [this.lng, this.lat]; }
    }

    function toLngLat(value) {
        if (value instanceof LngLat) return value;
        if (Array.isArray(value)) return new LngLat(value[0], value[1]);
        if (value && typeof value.getLng === 'function') {
            return new LngLat(value.getLng(), value.getLat());
        }
        if (value && value.lng !== undefined && value.lat !== undefined) {
            return new LngLat(value.lng, value.lat);
        }
        throw new TypeError('坐标必须为 [经度, 纬度]');
    }

    function toLeaflet(value) {
        const point = toLngLat(value);
        return L.latLng(point.lat, point.lng);
    }

    function fromLeaflet(value) {
        return new LngLat(value.lng, value.lat);
    }

    function pathToLeaflet(path) {
        return (path || []).map(toLeaflet);
    }

    function eventPayload(event) {
        const payload = Object.assign({}, event);
        if (event && event.latlng) payload.lnglat = fromLeaflet(event.latlng);
        if (event && event.containerPoint) {
            payload.pixel = new Pixel(event.containerPoint.x, event.containerPoint.y);
        }
        return payload;
    }

    function eventStore(owner) {
        if (!owner.__offlineEventHandlers) owner.__offlineEventHandlers = new Map();
        return owner.__offlineEventHandlers;
    }

    function rememberHandler(owner, type, original, wrapped) {
        const store = eventStore(owner);
        if (!store.has(type)) store.set(type, new Map());
        store.get(type).set(original, wrapped);
    }

    function recalledHandler(owner, type, original) {
        const handlers = eventStore(owner).get(type);
        return handlers ? handlers.get(original) : null;
    }

    class OfflineTileLayer {
        constructor(options) {
            this.options = options || {};
            this._isOfflineBaseLayer = true;
            this._leaflet = L.tileLayer(config.tileUrl, {
                minZoom: config.minZoom,
                maxZoom: config.maxZoom,
                maxNativeZoom: config.maxZoom,
                minNativeZoom: config.minZoom,
                noWrap: true,
                keepBuffer: 3,
                updateWhenIdle: false,
                errorTileUrl: transparentTile,
                attribution: '离线高德地图',
            });
        }
    }

    class EmptyTileLayer {
        constructor() {
            this._isOfflineBaseLayer = false;
            this._isEmptyLayer = true;
        }
    }

    class OfflineMap {
        constructor(container, options) {
            this.options = options || {};
            this._container = typeof container === 'string'
                ? document.getElementById(container)
                : container;
            if (!this._container) throw new Error('地图容器不存在');

            this._container.classList.add('offline-amap');
            this._leaflet = L.map(this._container, {
                attributionControl: false,
                zoomControl: true,
                minZoom: config.minZoom,
                maxZoom: config.maxZoom,
                zoomSnap: 1,
                worldCopyJump: false,
            });
            this._baseLayers = [];
            this.setLayers([new OfflineTileLayer()]);
            this._leaflet.setView(
                toLeaflet(this.options.center || [113.280637, 23.125178]),
                Math.max(config.minZoom, Math.min(config.maxZoom, this.options.zoom || 9)),
                {animate: false}
            );

            if (global.ResizeObserver) {
                this._resizeObserver = new ResizeObserver(() => {
                    this._leaflet.invalidateSize({pan: false});
                });
                this._resizeObserver.observe(this._container);
            }
        }

        on(type, handler) {
            const wrapped = event => handler(eventPayload(event));
            rememberHandler(this, type, handler, wrapped);
            this._leaflet.on(type, wrapped);
            return this;
        }

        off(type, handler) {
            if (!handler) {
                this._leaflet.off(type);
                eventStore(this).delete(type);
                return this;
            }
            const wrapped = recalledHandler(this, type, handler);
            if (wrapped) this._leaflet.off(type, wrapped);
            return this;
        }

        getZoom() { return this._leaflet.getZoom(); }

        setZoomAndCenter(zoom, center) {
            this._leaflet.setView(toLeaflet(center), zoom, {animate: false});
            return this;
        }

        panBy(x, y) {
            this._leaflet.panBy([Number(x) || 0, Number(y) || 0], {animate: false});
            return this;
        }

        setDefaultCursor(cursor) {
            this._container.style.cursor = cursor || 'default';
            this._container.querySelectorAll('.leaflet-interactive').forEach(element => {
                element.style.cursor = cursor || '';
            });
            return this;
        }

        setLayers(layers) {
            this._baseLayers.forEach(layer => {
                if (this._leaflet.hasLayer(layer)) this._leaflet.removeLayer(layer);
            });
            this._baseLayers = [];
            (layers || []).forEach(layer => {
                if (!layer || !layer._leaflet || layer._isEmptyLayer) return;
                if (this._baseLayers.length > 0 && layer._isOfflineBaseLayer) return;
                layer._leaflet.addTo(this._leaflet);
                this._baseLayers.push(layer._leaflet);
            });
            if (!this._baseLayers.length) {
                const fallback = new OfflineTileLayer();
                fallback._leaflet.addTo(this._leaflet);
                this._baseLayers.push(fallback._leaflet);
            }
            return this;
        }

        setFitView(overlays, immediately, padding, maxZoom) {
            const bounds = L.latLngBounds([]);
            (Array.isArray(overlays) ? overlays : [overlays]).forEach(overlay => {
                if (!overlay) return;
                if (typeof overlay._bounds === 'function') {
                    const overlayBounds = overlay._bounds();
                    if (overlayBounds && overlayBounds.isValid()) bounds.extend(overlayBounds);
                } else if (overlay._position) {
                    bounds.extend(toLeaflet(overlay._position));
                }
            });
            if (!bounds.isValid()) return this;
            const margins = Array.isArray(padding) ? padding : [20, 20, 20, 20];
            this._leaflet.fitBounds(bounds, {
                paddingTopLeft: [margins[3] || 0, margins[0] || 0],
                paddingBottomRight: [margins[1] || 0, margins[2] || 0],
                maxZoom: Math.min(Number(maxZoom) || config.maxZoom, config.maxZoom),
                animate: !immediately,
            });
            return this;
        }
    }

    class Overlay {
        constructor(options) {
            this.options = Object.assign({}, options || {});
            this._map = null;
            this._visible = true;
            this._leaflet = null;
        }

        _ensureLayer() {
            if (!this._leaflet) this._leaflet = this._createLayer();
            return this._leaflet;
        }

        setMap(map) {
            if (this._map && this._leaflet && this._map._leaflet.hasLayer(this._leaflet)) {
                this._map._leaflet.removeLayer(this._leaflet);
            }
            this._map = map || null;
            if (this._map && this._visible) this._ensureLayer().addTo(this._map._leaflet);
            return this;
        }

        show() {
            this._visible = true;
            if (this._map && !this._map._leaflet.hasLayer(this._ensureLayer())) {
                this._leaflet.addTo(this._map._leaflet);
            }
            return this;
        }

        hide() {
            this._visible = false;
            if (this._map && this._leaflet && this._map._leaflet.hasLayer(this._leaflet)) {
                this._map._leaflet.removeLayer(this._leaflet);
            }
            return this;
        }

        on(type, handler) {
            const layer = this._ensureLayer();
            const wrapped = event => handler(eventPayload(event));
            rememberHandler(this, type, handler, wrapped);
            layer.on(type, wrapped);
            return this;
        }

        off(type, handler) {
            if (!this._leaflet) return this;
            const wrapped = recalledHandler(this, type, handler);
            this._leaflet.off(type, wrapped || handler);
            return this;
        }

        _bounds() {
            if (this._leaflet && typeof this._leaflet.getBounds === 'function') {
                return this._leaflet.getBounds();
            }
            return this._position
                ? L.latLngBounds([toLeaflet(this._position)])
                : L.latLngBounds([]);
        }
    }

    class Icon {
        constructor(options) {
            this.options = Object.assign({}, options || {});
        }
    }

    function sizePair(value) {
        if (!value) return [0, 0];
        return [Number(value.width) || 0, Number(value.height) || 0];
    }

    function offsetTransform(options, dimensions) {
        const offset = options.offset || new Pixel(0, 0);
        const anchor = options.anchor || (options.icon ? 'center' : 'top-left');
        let x = offset.x;
        let y = offset.y;
        if (anchor.includes('center')) x -= dimensions[0] / 2;
        if (anchor.startsWith('bottom')) y -= dimensions[1];
        else if (anchor.startsWith('middle') || anchor === 'center') y -= dimensions[1] / 2;
        return `translate(${x}px, ${y}px)`;
    }

    class Marker extends Overlay {
        constructor(options) {
            super(options);
            this._position = toLngLat(this.options.position);
            this._content = this.options.content || null;
            this._icon = this.options.icon || null;
            this._angle = Number(this.options.angle) || 0;
            this._extData = this.options.extData;
            if (this.options.map) this.setMap(this.options.map);
        }

        _makeBody() {
            const body = document.createElement('div');
            body.className = 'offline-amap-marker-body';
            let dimensions = [0, 0];

            if (this._content instanceof Node) {
                body.appendChild(this._content);
                dimensions = [this._content.offsetWidth || 0, this._content.offsetHeight || 0];
            } else if (this._content !== null && this._content !== undefined) {
                body.innerHTML = String(this._content);
            } else if (this._icon instanceof Icon) {
                const image = document.createElement('img');
                image.src = this._icon.options.image || '';
                image.alt = this.options.title || '';
                dimensions = sizePair(this._icon.options.imageSize || this._icon.options.size);
                if (dimensions[0]) image.width = dimensions[0];
                if (dimensions[1]) image.height = dimensions[1];
                body.appendChild(image);
            }

            body.style.transform = `${offsetTransform(this.options, dimensions)} rotate(${this._angle}deg)`;
            body.style.cursor = this.options.cursor || 'pointer';
            if (this.options.title) body.title = this.options.title;
            return body;
        }

        _createLayer() {
            const icon = L.divIcon({
                className: 'offline-amap-marker',
                html: this._makeBody(),
                iconSize: [0, 0],
                iconAnchor: [0, 0],
            });
            return L.marker(toLeaflet(this._position), {
                icon,
                keyboard: false,
                bubblingMouseEvents: this.options.bubble === true,
                zIndexOffset: Number(this.options.zIndex) || 0,
                title: this.options.title || '',
            });
        }

        _refreshIcon() {
            if (!this._leaflet) return;
            this._leaflet.setIcon(L.divIcon({
                className: 'offline-amap-marker',
                html: this._makeBody(),
                iconSize: [0, 0],
                iconAnchor: [0, 0],
            }));
        }

        setPosition(position) {
            this._position = toLngLat(position);
            if (this._leaflet) this._leaflet.setLatLng(toLeaflet(this._position));
            return this;
        }
        getPosition() { return this._position; }
        getContent() { return this._content; }
        getExtData() { return this._extData; }
        setExtData(value) { this._extData = value; return this; }
        setAngle(angle) { this._angle = Number(angle) || 0; this._refreshIcon(); return this; }
        setIcon(icon) { this._icon = icon; this._content = null; this._refreshIcon(); return this; }
    }

    class Text extends Marker {
        constructor(options) {
            const textOptions = Object.assign({anchor: 'center'}, options || {});
            const targetMap = textOptions.map || null;
            delete textOptions.map;
            super(textOptions);
            this._text = String(textOptions.text || '');
            if (targetMap) this.setMap(targetMap);
        }

        _makeBody() {
            const body = document.createElement('div');
            body.className = 'offline-amap-marker-body offline-amap-text';
            body.textContent = this._text;
            Object.assign(body.style, this.options.style || {});
            const offset = this.options.offset || new Pixel(0, 0);
            const anchor = this.options.anchor || 'center';
            let anchorTransform = 'translate(-50%, -50%)';
            if (anchor === 'bottom-center') anchorTransform = 'translate(-50%, -100%)';
            if (anchor === 'top-center') anchorTransform = 'translate(-50%, 0)';
            body.style.transform = `${anchorTransform} translate(${offset.x}px, ${offset.y}px)`;
            body.style.cursor = this.options.cursor || 'default';
            if (this.options.title) body.title = this.options.title;
            return body;
        }
    }

    function pathStyle(options) {
        return {
            color: options.strokeColor || '#0066ff',
            weight: Number(options.strokeWeight) || 2,
            opacity: options.strokeOpacity === undefined ? 1 : Number(options.strokeOpacity),
            dashArray: options.strokeStyle === 'dashed' ? '8 8' : null,
            fillColor: options.fillColor || options.strokeColor || '#0066ff',
            fillOpacity: options.fillOpacity === undefined ? 0 : Number(options.fillOpacity),
            bubblingMouseEvents: options.bubble === true,
        };
    }

    class Polyline extends Overlay {
        constructor(options) {
            super(options);
            this._path = (this.options.path || []).map(toLngLat);
            if (this.options.map) this.setMap(this.options.map);
        }
        _createLayer() { return L.polyline(pathToLeaflet(this._path), pathStyle(this.options)); }
        getPath() { return this._path.slice(); }
        setOptions(options) {
            Object.assign(this.options, options || {});
            if (options && options.path) {
                this._path = options.path.map(toLngLat);
                if (this._leaflet) this._leaflet.setLatLngs(pathToLeaflet(this._path));
            }
            if (this._leaflet) this._leaflet.setStyle(pathStyle(this.options));
            return this;
        }
    }

    class Polygon extends Polyline {
        _createLayer() { return L.polygon(pathToLeaflet(this._path), pathStyle(this.options)); }
    }

    class CircleMarker extends Overlay {
        constructor(options) {
            super(options);
            this._position = toLngLat(this.options.center || this.options.position);
            if (this.options.map) this.setMap(this.options.map);
        }
        _createLayer() {
            return L.circleMarker(toLeaflet(this._position), Object.assign(
                pathStyle(this.options),
                {radius: Number(this.options.radius) || 5}
            ));
        }
        setPosition(position) {
            this._position = toLngLat(position);
            if (this._leaflet) this._leaflet.setLatLng(toLeaflet(this._position));
            return this;
        }
        setOptions(options) {
            Object.assign(this.options, options || {});
            if (this._leaflet) {
                this._leaflet.setStyle(pathStyle(this.options));
                if (options && options.radius !== undefined) this._leaflet.setRadius(options.radius);
            }
            return this;
        }
    }

    class InfoWindow {
        constructor(options) {
            this.options = options || {};
            this._content = '';
            this._map = null;
            const offset = this.options.offset || new Pixel(0, 0);
            this._leaflet = L.popup({
                className: 'offline-amap-popup',
                closeButton: false,
                autoClose: false,
                closeOnClick: false,
                offset: [offset.x, offset.y],
                maxWidth: 500,
            });
        }
        setContent(content) { this._content = content; this._leaflet.setContent(content); return this; }
        open(map, position) {
            this._map = map;
            this._leaflet.setLatLng(toLeaflet(position)).setContent(this._content).openOn(map._leaflet);
            return this;
        }
        close() {
            if (this._map) this._map._leaflet.closePopup(this._leaflet);
            this._map = null;
            return this;
        }
    }

    class EventEmitter {
        constructor() { this._handlers = new Map(); }
        on(type, handler) {
            if (!this._handlers.has(type)) this._handlers.set(type, new Set());
            this._handlers.get(type).add(handler);
            return this;
        }
        off(type, handler) {
            if (!handler) this._handlers.delete(type);
            else if (this._handlers.has(type)) this._handlers.get(type).delete(handler);
            return this;
        }
        emit(type, event) {
            (this._handlers.get(type) || []).forEach(handler => handler(event));
        }
    }

    class MouseTool extends EventEmitter {
        constructor(map) {
            super();
            this._map = map;
            this._points = [];
            this._draft = null;
            this._drawn = null;
            this._active = false;
            this._clickHandler = event => this._addPoint(event.lnglat);
            this._doubleClickHandler = event => this._finish(event.lnglat);
        }

        polygon(options) {
            this.close(true);
            this.options = Object.assign({}, options || {});
            this._active = true;
            this._map._leaflet.doubleClickZoom.disable();
            this._map.on('click', this._clickHandler);
            this._map.on('dblclick', this._doubleClickHandler);
            this._map.setDefaultCursor('crosshair');
            return this;
        }

        _addPoint(point) {
            if (!this._active) return;
            this._points.push(toLngLat(point));
            if (this._draft) this._draft.setMap(null);
            if (this._points.length >= 2) {
                this._draft = new Polyline(Object.assign({}, this.options, {
                    map: this._map,
                    path: this._points,
                    fillOpacity: 0,
                }));
            }
        }

        _finish(point) {
            if (!this._active) return;
            const finalPoint = toLngLat(point);
            const last = this._points[this._points.length - 1];
            if (!last || Math.abs(last.lng - finalPoint.lng) > 1e-9 ||
                    Math.abs(last.lat - finalPoint.lat) > 1e-9) {
                this._points.push(finalPoint);
            }
            const unique = this._points.filter((item, index, values) => index === 0 ||
                Math.abs(item.lng - values[index - 1].lng) > 1e-9 ||
                Math.abs(item.lat - values[index - 1].lat) > 1e-9);
            if (unique.length < 3) return;
            if (this._draft) this._draft.setMap(null);
            this._draft = null;
            this._drawn = new Polygon(Object.assign({}, this.options, {
                map: this._map,
                path: unique,
            }));
            this._active = false;
            this._detach();
            this.emit('draw', {obj: this._drawn});
        }

        _detach() {
            this._map.off('click', this._clickHandler);
            this._map.off('dblclick', this._doubleClickHandler);
            this._map._leaflet.doubleClickZoom.enable();
            this._map.setDefaultCursor('default');
        }

        close(clear) {
            if (this._active) this._detach();
            this._active = false;
            this._points = [];
            if (this._draft) this._draft.setMap(null);
            this._draft = null;
            if (clear && this._drawn) this._drawn.setMap(null);
            if (clear) this._drawn = null;
            return this;
        }
    }

    function createDefaultLayer() { return new OfflineTileLayer(); }

    function convertFrom(points, source, callback) {
        try {
            const list = Array.isArray(points) && !Array.isArray(points[0]) &&
                !(points[0] instanceof LngLat) ? [points] : points;
            const locations = (list || []).map(value => {
                const point = toLngLat(value).toArray();
                const coordinateConverter = typeof MaritimeCoordinates !== 'undefined'
                    ? MaritimeCoordinates
                    : global.MaritimeCoordinates;
                const converted = source === 'gps' && coordinateConverter
                    ? coordinateConverter.gpsToMap(point)
                    : point;
                return new LngLat(converted[0], converted[1]);
            });
            global.setTimeout(() => callback('complete', {info: 'ok', locations}), 0);
        } catch (error) {
            global.setTimeout(() => callback('error', {info: error.message, locations: []}), 0);
        }
    }

    const Event = {
        addListener(target, type, handler) {
            target.on(type, handler);
            return {target, type, handler};
        },
        removeListener(listener) {
            if (listener && listener.target) listener.target.off(listener.type, listener.handler);
        },
    };

    const offlineAMap = Object.freeze({
        __offlineProvider: true,
        Map: OfflineMap,
        Marker,
        Text,
        Icon,
        Size,
        Pixel,
        LngLat,
        Polygon,
        Polyline,
        CircleMarker,
        InfoWindow,
        MouseTool,
        TileLayer: Object.freeze({
            Satellite: OfflineTileLayer,
            RoadNet: EmptyTileLayer,
        }),
        Event,
        event: Event,
        createDefaultLayer,
        convertFrom,
    });
    global.OfflineAMap = offlineAMap;
    global.AMap = offlineAMap;
})(window);
