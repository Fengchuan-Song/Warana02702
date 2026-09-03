/*
 * WGS84 / GCJ-02 numerical conversion, adapted from eviltransform:
 * https://github.com/googollee/eviltransform/blob/master/javascript/transform.js
 * The iterative inverse uses the same local forward model as map rendering.
 * Numerical convergence does not imply survey-grade positional accuracy.
 *
 * Copyright (c) 2015, Googol Lee <i@googol.im>, @gutenye, @xingxing, @bewantbe,
 * @GhostFlying, @larryli, @gumblex,@lbt05, @chenweiyj
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 * 1. Redistributions of source code must retain the above copyright notice, this
 *    list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright notice,
 *    this list of conditions and the following disclaimer in the documentation
 *    and/or other materials provided with the distribution.
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
 * WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
 * ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
 * (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
 * ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
 * SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 * The views and conclusions contained in the software and documentation are those
 * of the authors and should not be interpreted as representing official policies,
 * either expressed or implied, of the FreeBSD Project.
 */
const MaritimeCoordinates = (() => {
    const earthRadius = 6378137.0;
    const eccentricitySquared = 0.00669342162296594323;

    function validate(point) {
        if (!Array.isArray(point) || point.length !== 2 ||
            point.some(value => typeof value !== 'number' || !Number.isFinite(value)) ||
            Math.abs(point[0]) > 180 || Math.abs(point[1]) > 90) {
            throw new Error('经纬度无效，请使用 [经度, 纬度]，经度范围 -180～180，纬度范围 -90～90。');
        }
        return point;
    }

    function outsideTransformBounds(lon, lat) {
        return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
    }

    function offset(lon, lat) {
        const x = lon - 105;
        const y = lat - 35;
        const xPi = x * Math.PI;
        const yPi = y * Math.PI;
        const common = 20 * Math.sin(6 * xPi) + 20 * Math.sin(2 * xPi);
        let latShift = common + 20 * Math.sin(yPi) + 40 * Math.sin(yPi / 3);
        let lonShift = common + 20 * Math.sin(xPi) + 40 * Math.sin(xPi / 3);
        latShift += 160 * Math.sin(yPi / 12) + 320 * Math.sin(yPi / 30);
        lonShift += 150 * Math.sin(xPi / 12) + 300 * Math.sin(xPi / 30);
        latShift = latShift * 2 / 3 - 100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * Math.sqrt(Math.abs(x));
        lonShift = lonShift * 2 / 3 + 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * Math.sqrt(Math.abs(x));
        const radians = lat * Math.PI / 180;
        const magic = 1 - eccentricitySquared * Math.sin(radians) ** 2;
        const root = Math.sqrt(magic);
        return [
            lonShift * 180 / (earthRadius / root * Math.cos(radians) * Math.PI),
            latShift * 180 / (earthRadius * (1 - eccentricitySquared) / (magic * root) * Math.PI),
        ];
    }

    function gpsToMap(point) {
        const [lon, lat] = validate(point);
        if (outsideTransformBounds(lon, lat)) return [...point];
        const [dx, dy] = offset(lon, lat);
        return [lon + dx, lat + dy];
    }

    function mapToGps(point) {
        const [lon, lat] = validate(point);
        if (outsideTransformBounds(lon, lat)) return [...point];
        let estimate = [...point];
        for (let attempt = 0; attempt < 30; attempt++) {
            const projected = gpsToMap(estimate);
            const dx = lon - projected[0];
            const dy = lat - projected[1];
            if (Math.max(Math.abs(dx), Math.abs(dy)) < 1e-9) return estimate;
            estimate = [estimate[0] + dx, estimate[1] + dy];
        }
        throw new Error('该位置无法稳定转换坐标，请使用 WGS84 坐标手动填写。');
    }

    return Object.freeze({gpsToMap, mapToGps});
})();
if (typeof module === 'object' && module.exports) module.exports = MaritimeCoordinates;
