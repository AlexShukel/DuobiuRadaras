// Mock pothole data source. Replace with a real data store / API later.

// Rough bounding box of Vilnius city.
const VILNIUS_BOUNDS = {
  minLat: 54.64,
  maxLat: 54.74,
  minLng: 25.18,
  maxLng: 25.34,
};

// Small seeded PRNG (mulberry32) so the mock returns the same points on every request.
function createRandom(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function getPotholes(count = 50, seed = 42) {
  const random = createRandom(seed);
  const { minLat, maxLat, minLng, maxLng } = VILNIUS_BOUNDS;

  return Array.from({ length: count }, (_, i) => ({
    id: i + 1,
    lat: +(minLat + random() * (maxLat - minLat)).toFixed(6),
    lng: +(minLng + random() * (maxLng - minLng)).toFixed(6),
  }));
}
