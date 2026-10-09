const VILNIUS_CENTER = [54.6872, 25.2797];
const POTHOLES_URL = '/api/potholes';

const map = L.map('map').setView(VILNIUS_CENTER, 12);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

const statusEl = document.getElementById('status');

function showStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle('error', isError);
  statusEl.hidden = false;
}

function renderPotholes(potholes) {
  const layer = L.layerGroup().addTo(map);

  for (const { lat, lng } of potholes) {
    L.circleMarker([lat, lng], {
      radius: 6,
      color: '#b00000',
      weight: 1,
      fillColor: '#ff0000',
      fillOpacity: 0.9,
    }).addTo(layer);
  }
}

async function loadPotholes() {
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

loadPotholes();
