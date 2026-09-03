// Run with: node scripts/test_maritime_coordinates.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const coordinates = require('../templates/maritime_coordinates.js');

// Independent known pairs published by the upstream project's test suite.
// https://github.com/googollee/eviltransform/blob/master/javascript/test.js
const fixtures = [
    [[121.5272106, 31.1774276], [121.531541859215, 31.17530398364597]],
    [[113.912316, 22.543847], [113.9171774808363, 22.540796131694766]],
    [[116.377817, 39.911954], [116.38404722455657, 39.91334545536069]],
];
function near(actual, expected, tolerance = 1e-8) {
    expected.forEach((number, axis) => assert(Math.abs(number - actual[axis]) < tolerance,
        `axis ${axis}: ${actual[axis]} differs from ${number}`));
}
for (const [gps, map] of fixtures) {
    // Upstream fixtures are validated to six decimal places (about 0.1 m).
    near(coordinates.gpsToMap(gps), map, 1e-6);
    near(coordinates.mapToGps(map), gps, 1e-6);
}
for (const point of [[114.17, 22.3], [113.55, 22.19], [116.25, 22.88], [0, 0], [-74, 40.7]]) {
    near(coordinates.mapToGps(coordinates.gpsToMap(point)), point);
}
for (const invalid of [[181, 22], [113, 91], [NaN, 22], [113, Infinity], [113], ['113', 22], null]) {
    assert.throws(() => coordinates.gpsToMap(invalid));
    assert.throws(() => coordinates.mapToGps(invalid));
}

// Reproduce the actual service error. None of the maritime conversions should
// call the failed service, including a 500-vertex polygon and its map layer.
let onlineCalls = 0;
const sandbox = {
    MaritimeCoordinates: coordinates,
    AMap: {convertFrom: (_points, _type, callback) => {
        onlineCalls++;
        callback('error', 'INVALID_USER_SCODE');
    }},
};
vm.createContext(sandbox);
const root = path.join(__dirname, '..');
vm.runInContext(fs.readFileSync(path.join(root, 'templates/maritime_zone_script.html'), 'utf8'), sandbox);
(async () => {
    const polygon = Array.from({length: 500}, (_, index) => {
        const angle = index / 500 * Math.PI * 2;
        return [113.8 + Math.cos(angle) * .01, 22.5 + Math.sin(angle) * .01];
    });
    const converted = await sandbox.convertMaritimeGpsStrict(polygon);
    const restored = await sandbox.maritimeMapToGps(converted);
    polygon.forEach((point, index) => near(restored[index], point));
    const template = fs.readFileSync(path.join(root, 'templates/Demo_v10.html'), 'utf8');
    const render = template.slice(template.indexOf('async function renderMaritimeZones'), template.indexOf('function loadMaritimeZones'));
    assert(render.includes('await convertMaritimeGpsStrict('));
    assert.equal(onlineCalls, 0);
    console.log('Coordinate regression passed: reference pairs, Guangdong/HK/Macau, invalid inputs, 500 vertices, INVALID_USER_SCODE.');
})().catch(error => { console.error(error); process.exitCode = 1; });
