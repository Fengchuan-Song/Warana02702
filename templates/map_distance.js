(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.MapDistance = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    const EARTH_RADIUS_METRES = 6371000;

    function normalisePoint(point) {
        if (!Array.isArray(point) || point.length < 2) {
            throw new TypeError('坐标必须是 [经度, 纬度]');
        }
        const longitude = Number(point[0]);
        const latitude = Number(point[1]);
        if (
            !Number.isFinite(longitude)
            || !Number.isFinite(latitude)
            || longitude < -180
            || longitude > 180
            || latitude < -90
            || latitude > 90
        ) {
            throw new RangeError('经纬度超出有效范围');
        }
        return [longitude, latitude];
    }

    function distanceMetres(firstPoint, secondPoint) {
        const first = normalisePoint(firstPoint);
        const second = normalisePoint(secondPoint);
        const radians = degrees => degrees * Math.PI / 180;
        const latitude1 = radians(first[1]);
        const latitude2 = radians(second[1]);
        const latitudeDelta = latitude2 - latitude1;
        const longitudeDelta = radians(second[0] - first[0]);
        const haversine = (
            Math.sin(latitudeDelta / 2) ** 2
            + Math.cos(latitude1)
            * Math.cos(latitude2)
            * Math.sin(longitudeDelta / 2) ** 2
        );
        return 2 * EARTH_RADIUS_METRES * Math.asin(
            Math.sqrt(Math.min(1, Math.max(0, haversine)))
        );
    }

    function totalDistanceMetres(points) {
        if (!Array.isArray(points)) {
            throw new TypeError('测距点必须是坐标数组');
        }
        let total = 0;
        for (let index = 1; index < points.length; index += 1) {
            total += distanceMetres(points[index - 1], points[index]);
        }
        return total;
    }

    function formatDistance(distance) {
        const metres = Number(distance);
        if (!Number.isFinite(metres) || metres < 0) {
            throw new RangeError('距离必须是非负有限数值');
        }
        if (metres < 1000) return `${metres.toFixed(1)} 米`;
        const kilometres = metres / 1000;
        return `${kilometres.toFixed(kilometres < 10 ? 2 : 1)} 公里`;
    }

    return Object.freeze({distanceMetres, totalDistanceMetres, formatDistance});
});
