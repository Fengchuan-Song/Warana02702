// Run with: node scripts/test_map_distance.cjs
const assert = require('node:assert/strict');
const distance = require('../templates/map_distance.js');

assert.equal(distance.distanceMetres([113.9, 22.4], [113.9, 22.4]), 0);

// One degree of longitude on the equator is about 111.195 km.
const equatorDegree = distance.distanceMetres([0, 0], [1, 0]);
assert(Math.abs(equatorDegree - 111194.93) < 0.1);
assert(Math.abs(
    distance.distanceMetres([114.0, 22.5], [114.01, 22.5])
    - distance.distanceMetres([114.01, 22.5], [114.0, 22.5])
) < 1e-9);
const multiPointTotal = distance.totalDistanceMetres([
    [0, 0],
    [1, 0],
    [1, 1],
]);
assert(Math.abs(multiPointTotal - 222389.85) < 0.2);
assert.equal(distance.totalDistanceMetres([]), 0);
assert.equal(distance.totalDistanceMetres([[114, 22]]), 0);

assert.equal(distance.formatDistance(0), '0.0 米');
assert.equal(distance.formatDistance(999.94), '999.9 米');
assert.equal(distance.formatDistance(1234), '1.23 公里');
assert.equal(distance.formatDistance(12345), '12.3 公里');
assert.throws(() => distance.distanceMetres([181, 22], [114, 22]));
assert.throws(() => distance.distanceMetres([114], [114, 22]));
assert.throws(() => distance.totalDistanceMetres(null));
assert.throws(() => distance.formatDistance(-1));

console.log('Map distance regression passed.');
