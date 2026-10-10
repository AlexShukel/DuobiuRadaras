const VILNIUS_CENTER = [54.6872, 25.2797];
const POTHOLES_URL = '/api/potholes';
const DATASET_URL = '/api/dataset';

const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILE_OPTIONS = {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
};

const POTHOLE_STYLE = { radius: 6, color: '#b00000', weight: 1, fillColor: '#ff0000', fillOpacity: 0.9 };
const BUMP_STYLE = { radius: 6, color: '#6a0dad', weight: 1, fillColor: '#a020f0', fillOpacity: 0.9 };
// One colour per phone (device) so the two cars' routes can be told apart.
const ROUTE_COLORS = ['#1f77b4', '#2ca02c', '#ff7f0e', '#17becf', '#8c564b'];

function createMap(id) {
  const map = L.map(id).setView(VILNIUS_CENTER, 12);
  L.tileLayer(TILE_URL, TILE_OPTIONS).addTo(map);
  return map;
}

function makeStatus(id) {
  const el = document.getElementById(id);
  return (text, isError = false) => {
    el.textContent = text;
    el.classList.toggle('error', isError);
    el.hidden = false;
  };
}

// ---- Potholes tab (live detections from the server) ----

let map = null;
const showStatus = makeStatus('status');

function renderPotholes(potholes) {
  const layer = L.layerGroup().addTo(map);

  for (const { lat, lng } of potholes) {
    L.circleMarker([lat, lng], POTHOLE_STYLE).addTo(layer);
  }
}

async function loadPotholes() {
  map = createMap('map');
  try {
    const response = await fetch(POTHOLES_URL);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const potholes = await response.json();
    renderPotholes(potholes);
    showStatus(`${potholes.length} potholes`);
  } catch (err) {
    console.error('Failed to load potholes', err);
    showStatus('Failed to load potholes', true);
  }
}

// ---- Training data tab (routes and human labels from server/devices) ----

let datasetMap = null;
const showDatasetStatus = makeStatus('dataset-status');

function formatTime(timestamp) {
  return new Date(timestamp).toLocaleString();
}

function renderDataset({ devices }) {
  const routesEl = document.getElementById('dataset-routes');
  const bounds = L.latLngBounds([]);
  const routeLayer = L.layerGroup().addTo(datasetMap);
  const labelLayer = L.layerGroup().addTo(datasetMap);
  let potholes = 0;
  let bumps = 0;

  devices.forEach((device, i) => {
    const color = ROUTE_COLORS[i % ROUTE_COLORS.length];
    // Phones that drove together share the road, so each later route is drawn thinner on top
    // of the previous ones and both stay visible.
    const weight = Math.max(2, 7 - 2.5 * i);

    for (const segment of device.route) {
      const line = L.polyline(segment, { color, weight, opacity: 0.85 }).addTo(routeLayer);
      line.bindTooltip(`${device.ip}`, { sticky: true });
      bounds.extend(line.getBounds());
    }

    for (const { lat, lng, label, timestamp } of device.labels) {
      const isPothole = label === 'pothole';
      if (isPothole) potholes += 1;
      else if (label === 'bump') bumps += 1;
      else continue;
      L.circleMarker([lat, lng], isPothole ? POTHOLE_STYLE : BUMP_STYLE)
        .bindPopup(`<b>${label}</b><br>${device.ip}<br>${formatTime(timestamp)}`)
        .addTo(labelLayer);
      bounds.extend([lat, lng]);
    }

    const row = document.createElement('div');
    row.className = 'legend-row';
    const swatch = document.createElement('span');
    swatch.className = 'line';
    swatch.style.background = color;
    const text = document.createElement('span');
    const span = device.from && device.to
      ? ` (${formatTime(device.from)} – ${new Date(device.to).toLocaleTimeString()})`
      : '';
    text.textContent = `${device.ip}${span}`;
    row.append(swatch, text);
    routesEl.append(row);
  });

  if (bounds.isValid()) {
    datasetMap.fitBounds(bounds, { padding: [30, 30] });
  }
  document.getElementById('dataset-legend').hidden = devices.length === 0;
  showDatasetStatus(`${devices.length} devices · ${potholes} potholes · ${bumps} bumps`);
}

async function loadDataset() {
  datasetMap = createMap('dataset-map');
  try {
    const response = await fetch(DATASET_URL);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    renderDataset(await response.json());
  } catch (err) {
    console.error('Failed to load training data', err);
    showDatasetStatus('Failed to load training data', true);
  }
}

// ---- Tabs ----

const loaders = { live: loadPotholes, dataset: loadDataset };
const loaded = new Set();
const maps = { live: () => map, dataset: () => datasetMap };

function activateTab(name) {
  for (const tab of document.querySelectorAll('.tab')) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  }
  for (const panel of document.querySelectorAll('.panel')) {
    const active = panel.id === `panel-${name}`;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  }
  if (!loaded.has(name)) {
    loaded.add(name);
    loaders[name]();
  }
  // A map created or resized while its panel was hidden has a stale size; recompute now that it is visible.
  maps[name]()?.invalidateSize();
  if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);
}

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => activateTab(tab.dataset.tab));
}

activateTab(location.hash === '#dataset' ? 'dataset' : 'live');
