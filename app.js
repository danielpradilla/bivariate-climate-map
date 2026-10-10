/* Regional adaptation of Milan Janosov's TerraClimate bivariate method.
 * Palette and percentile choices: https://milanjanosov.substack.com/p/bivariate-climate-map-temperature
 * Earlier work: Muhammad Mohsin Raza and Philippe Rivière. Full credits in index.html.
 */
const d3 = window.d3;
const $ = (id) => document.getElementById(id);
const palette = [
  ['#3a9dbf', '#2a7d82', '#1e6050', '#164427'],
  ['#7ab5c8', '#6d9490', '#5e7a62', '#4a5a30'],
  ['#b0cdd6', '#a8b5aa', '#9e9278', '#9a6e3a'],
  ['#d8d0c8', '#dbbfa4', '#dc9e72', '#dd6a2b'],
];
let cities = [];
const canvas = $('map');
const context = canvas.getContext('2d');
const raster = document.createElement('canvas');
const rasterContext = raster.getContext('2d');
let metadata, raw, smoothed, geography, projection, path, pixelCells, image, cellIndices;
let registry, region, dataset, pendingRegion = null, regionController = null;
let projectionRotation, projectionClip = 180;
let width = 0, height = 0, ratio = 1, frame = 0;
let transform = d3.zoomIdentity;
let selected = null;
let highlighted = null;
let breaksT, breaksP, classes;
let zoom;
let lookupDirty = false;
let currentYear = null, currentMetadata = null, pendingYear = null, requestedYear = null;
let requestToken = 0, playing = false, playbackTimer = 0;
const cacheBytesLimit = 48 * 1024 * 1024;
let rippleAnimations = [], rippleTimer = 0, rippleActive = false;
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const littleEndian = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
const number = new Intl.NumberFormat('en');
const classify = (value, breaks) => value < breaks[1] ? 0 : value < breaks[2] ? 1 : value < breaks[3] ? 2 : 3;
const colors = (mode) => Array.from({ length: 16 }, (_, i) => {
  const t = i % 4, p = i >> 2;
  const color = mode === 'temperature' ? palette[3][t] : mode === 'precipitation' ? palette[3 - p][0] : palette[3 - p][t];
  return d3.color(color);
});
function cellAt(lon, lat) {
  const delta = ((lon - metadata.west) % 360 + 360) % 360;
  let x = Math.round(delta / metadata.dx);
  const row = (metadata.north - lat) / metadata.dy;
  // Cell footprints include their outer half-cell edge, including the geographic poles.
  if (row < -0.5 - 1e-9 || row > metadata.height - 0.5 + 1e-9) return -1;
  const y = Math.max(0, Math.min(metadata.height - 1, Math.round(row)));
  if (metadata.width * metadata.dx >= 359.99) x %= metadata.width;
  if (x < 0 || y < 0 || x >= metadata.width || y >= metadata.height) return -1;
  const index = y * metadata.width + x;
  if (!cellIndices) return index;
  let low = 0, high = cellIndices.length - 1;
  while (low <= high) {
    const mid = (low + high) >>> 1, value = cellIndices[mid];
    if (value === index) return mid;
    if (value < index) low = mid + 1;
    else high = mid - 1;
  }
  return -1;
}
function coordinates(i) {
  const index = cellIndices ? cellIndices[i] : i;
  const lon = metadata.west + (index % metadata.width) * metadata.dx;
  return [((lon + 180) % 360 + 360) % 360 - 180,
    metadata.north - Math.floor(index / metadata.width) * metadata.dy];
}
const latitudeText = (lat, digits) => `${Math.abs(lat).toFixed(digits)}° ${lat < 0 ? 'S' : 'N'}`;
const longitudeText = (lon, digits) => `${Math.abs(lon).toFixed(digits)}° ${lon < 0 ? 'W' : 'E'}`;
const baselineText = () => `${metadata.years[0]}–${metadata.years.at(-1)}`;
const projectionName = () => region.projection.type === 'equalEarth' ? 'Equal Earth'
  : region.projection.type === 'stereographic' ? 'Stereographic' : 'Lambert azimuthal equal-area';
function visibleCoordinate(coordinate) {
  if (!coordinate || !coordinate.every(Number.isFinite) || Math.abs(coordinate[1]) > 90) return false;
  if (projectionClip >= 180) return true;
  const rotated = projectionRotation(coordinate);
  return Math.cos(rotated[0] * Math.PI / 180) * Math.cos(rotated[1] * Math.PI / 180)
    > Math.cos(projectionClip * Math.PI / 180);
}
function inverseCoordinate(point) {
  const coordinate = projection.invert(point);
  if (!visibleCoordinate(coordinate)) return null;
  const projected = projection(coordinate);
  return projected && Math.hypot(projected[0] - point[0], projected[1] - point[1]) < 0.1 ? coordinate : null;
}
function cancelRipple() {
  clearTimeout(rippleTimer);
  rippleAnimations.forEach(animation => animation.cancel());
  rippleAnimations = [];
  rippleActive = false;
  if ($('city-ripple')) $('city-ripple').hidden = true;
}
function positionRipple() {
  if (!rippleActive || !selected || !projection) return;
  const point = transform.apply(projection(selected.coordinate));
  $('city-ripple').style.transform = `translate(${point[0]}px, ${point[1]}px)`;
}
function startRipple() {
  cancelRipple();
  const wrapper = $('city-ripple');
  if (!wrapper || reducedMotion.matches || !selected) return;
  rippleActive = true;
  wrapper.hidden = false;
  positionRipple();
  rippleAnimations = [...wrapper.querySelectorAll('.ripple-ring')].map((ring, index) =>
    ring.animate([{ transform: 'scale(1)', opacity: 0.75 }, { transform: 'scale(6)', opacity: 0 }],
      { duration: 2600, delay: index * 400, easing: 'ease-out', fill: 'none' }));
  rippleTimer = setTimeout(cancelRipple, 3000);
}
reducedMotion.addEventListener('change', event => { if (event.matches) cancelRipple(); });
function rangeText(breaks, bin, unit) {
  const digits = unit === '°C' ? 1 : 0;
  return `${breaks[bin].toFixed(digits)} to ${breaks[bin + 1].toFixed(digits)} ${unit}`;
}
function classText(t, p) {
  return `${rangeText(breaksT, t, '°C')} · ${rangeText(breaksP, p, 'mm/year')}`;
}
function selectClass(index) {
  highlighted = highlighted === index ? null : index;
  updateLegend();
  paintRaster();
  scheduleDraw();
}
function updateLegend() {
  const mode = $('mode').value;
  const grid = $('legend-grid');
  const focusedClass = grid.contains(document.activeElement) ? document.activeElement.dataset.class : undefined;
  grid.replaceChildren();
  if (!raw) {
    $('legend-title').textContent = 'Climate coverage unavailable';
    $('legend-intro').textContent = metadata.coverageNote || region.coverageNote || 'No complete annual climate frames are available.';
    for (const id of ['legend-x-label', 'legend-y-label', 'legend-detail', 'classification-note', 'smoothing-note', 'threshold-values']) $(id).replaceChildren();
    grid.parentElement.setAttribute('aria-label', 'No climate classes: annual data unavailable.');
    return;
  }
  const showT = mode !== 'precipitation';
  const showP = mode !== 'temperature';
  grid.style.gridTemplateColumns = `repeat(${showT ? 4 : 1}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${showP ? 4 : 1}, 1fr)`;
  grid.parentElement.setAttribute('aria-label', mode === 'bivariate'
    ? 'Temperature increases to the right; precipitation increases upward.'
    : showT ? 'Temperature increases to the right.' : 'Precipitation increases upward.');
  $('legend-title').textContent = mode === 'bivariate' ? 'Temperature × precipitation' : showT ? 'Maximum temperature' : 'Annual precipitation';
  $('legend-intro').textContent = mode === 'bivariate'
    ? `Each colour combines ${currentYear} warmth and precipitation against the pooled ${baselineText()} ${region.label} annual baseline.`
    : showT ? `Annual mean monthly maximum temperature for ${currentYear}, in degrees Celsius.`
      : `Total precipitation over ${currentYear}, in millimetres.`;
  for (let p = showP ? 3 : 0; p >= 0; p--) {
    for (let t = 0; t < (showT ? 4 : 1); t++) {
      const index = p * 4 + t;
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.class = index;
      button.style.background = mode === 'temperature' ? palette[3][t] : mode === 'precipitation' ? palette[3 - p][0] : palette[3 - p][t];
      const label = mode === 'temperature' ? rangeText(breaksT, t, '°C') : mode === 'precipitation' ? rangeText(breaksP, p, 'mm/year') : classText(t, p);
      button.setAttribute('aria-label', label);
      button.title = label;
      button.setAttribute('aria-pressed', String(highlighted === index));
      button.addEventListener('click', () => selectClass(index));
      if (mode === 'bivariate' && (p === 0 || p === 3) && (t === 0 || t === 3)) {
        const corner = document.createElement('span');
        corner.className = `corner-label${p === 3 && t === 3 ? ' light' : ''}`;
        corner.textContent = `${t === 0 ? 'Cooler' : 'Warmer'}\n${p === 0 ? 'Drier' : 'Wetter'}`;
        button.append(corner);
      }
      grid.append(button);
    }
  }
  if (focusedClass !== undefined) grid.querySelector(`[data-class="${focusedClass}"]`)?.focus({ preventScroll: true });
  $('legend-x-label').innerHTML = showT ? '<span>Maximum temperature</span><span>Warmer →</span>' : '';
  $('legend-y-label').innerHTML = showP ? '<span>Wetter</span><span aria-hidden="true">↑</span><span>Annual precipitation</span>' : '';
  $('classification-note').textContent = ($('classification').value === 'equal'
    ? 'Quartiles contain approximately equal numbers of pooled raw annual grid cells, not equal land areas.'
    : `Low and high are relative. The original classes contain unequal shares of pooled raw annual ${region.label} cells.`)
    + ` Fixed baseline: ${baselineText()}.`;
  $('smoothing-note').textContent = $('smoothing').checked
    ? `Gaussian σ=${metadata.smoothing.sigmaCells} selected-grid cells${region.id === 'world' ? ' on the stride-6 World grid' : ''}; inspection shows original values.`
    : 'Original grid cells; no smoothing applied.';
  $('legend-detail').textContent = highlighted === null
    ? ''
    : `${mode === 'temperature' ? rangeText(breaksT, highlighted % 4, '°C') : mode === 'precipitation' ? rangeText(breaksP, highlighted >> 2, 'mm/year') : classText(highlighted % 4, highlighted >> 2)}. Other cells are faded.`;
}
function updateClasses() {
  if (!raw) return;
  const equal = $('classification').value === 'equal';
  breaksT = equal ? metadata.equalTemperatureBreaks : metadata.temperatureBreaks;
  breaksP = equal ? metadata.equalPrecipitationBreaks : metadata.precipitationBreaks;
  const values = $('smoothing').checked ? smoothed : raw;
  if (!classes || classes.length !== raw.length / 2) classes = new Uint8Array(raw.length / 2);
  for (let i = 0; i < classes.length; i++) {
    classes[i] = raw[i * 2] === -32768 || raw[i * 2 + 1] === -32768 ? 255 : classify(values[i * 2] / 100, breaksT) + 4 * classify(values[i * 2 + 1], breaksP);
  }
  $('threshold-values').innerHTML = `<table><caption>Pooled annual ${region.label} land cells, ${baselineText()}</caption><thead><tr><th>Variable</th><th>Breaks</th></tr></thead><tbody><tr><th>Temperature</th><td>${breaksT.map(v => v.toFixed(1)).join(' / ')} °C</td></tr><tr><th>Precipitation</th><td>${breaksP.map(v => number.format(Math.round(v))).join(' / ')} mm</td></tr></tbody></table><p>${equal ? `Both variables: ${metadata.equalPercentiles.join(', ')} percentiles.` : `Temperature: ${metadata.temperaturePercentiles.join(', ')}. Precipitation: ${metadata.precipitationPercentiles.join(', ')} percentiles.`} Fixed thresholds from encoded raw annual cells across ${baselineText()}, never recalculated for the displayed year. Unweighted cells; not equal land areas. Boundary values enter the higher class. Displayed thresholds are rounded.</p>`;
  updateLegend();
  paintRaster();
  updateInspector();
  scheduleDraw();
}
function makePixelLookup() {
  const w = canvas.width, h = canvas.height;
  raster.width = w;
  raster.height = h;
  image = rasterContext.createImageData(w, h);
  pixelCells = new Int32Array(w * h).fill(-1);
  if (raw) for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const point = transform.invert([(x + 0.5) / ratio, (y + 0.5) / ratio]);
      const location = inverseCoordinate(point);
      if (location) pixelCells[y * w + x] = cellAt(...location);
    }
  }
  paintRaster();
}
function paintRaster() {
  if (!pixelCells || !image) return;
  if (!classes) { image.data.fill(0); rasterContext.putImageData(image, 0, 0); return; }
  const mode = $('mode').value;
  const rgb = colors(mode);
  const bytes = image.data;
  for (let i = 0; i < pixelCells.length; i++) {
    const cell = pixelCells[i];
    if (cell < 0 || classes[cell] === 255) { bytes[i * 4 + 3] = 0; continue; }
    const category = classes[cell];
    const color = rgb[category];
    const match = highlighted === null || (mode === 'temperature' ? category % 4 === highlighted % 4 : mode === 'precipitation' ? (category >> 2) === (highlighted >> 2) : category === highlighted);
    bytes[i * 4] = color.r;
    bytes[i * 4 + 1] = color.g;
    bytes[i * 4 + 2] = color.b;
    bytes[i * 4 + 3] = match ? 255 : 35;
  }
  rasterContext.putImageData(image, 0, 0);
}
function scheduleDraw(reproject = false) {
  if (reproject === true) lookupDirty = true;
  if (!frame) frame = requestAnimationFrame(() => {
    frame = 0;
    if (lookupDirty && projection) { makePixelLookup(); lookupDirty = false; }
    draw();
  });
}
function flushDraw() {
  if (frame) cancelAnimationFrame(frame);
  frame = 0;
  if (lookupDirty && projection) { makePixelLookup(); lookupDirty = false; }
  draw();
}
function draw() {
  if (!projection) return;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.fillStyle = '#fffdf9';
  context.fillRect(0, 0, canvas.width, canvas.height);
  if (raster.width && raster.height) context.drawImage(raster, 0, 0);
  context.setTransform(ratio * transform.k, 0, 0, ratio * transform.k, ratio * transform.x, ratio * transform.y);
  if ($('borders').checked || !raw) {
    context.beginPath();
    path(geography);
    context.strokeStyle = raw && region.id === 'europe' ? 'rgba(255,253,249,0.75)' : raw ? 'rgba(80,90,95,0.5)' : '#687177';
    context.lineWidth = 0.65 / transform.k;
    context.stroke();
  }
  if (selected && visibleCoordinate(selected.coordinate)) {
    const point = projection(selected.coordinate);
    context.beginPath();
    context.arc(point[0], point[1], 4.5 / transform.k, 0, 2 * Math.PI);
    context.fillStyle = '#fffdf9';
    context.fill();
    context.strokeStyle = '#202b32';
    context.lineWidth = 1.3 / transform.k;
    context.stroke();
  }
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.font = '10px -apple-system, BlinkMacSystemFont, sans-serif';
  context.fillStyle = '#687177';
  context.fillText(`${projectionName()}${currentYear === null ? ' · no annual coverage' : ` · ${currentYear}`}`, 18, height - 38);
  positionRipple();
}
function resize() {
  const box = $('map-wrap').getBoundingClientRect();
  const newWidth = Math.round(box.width), newHeight = Math.round(box.height);
  if (!geography || !newWidth || !newHeight || (newWidth === width && newHeight === height && projection)) return;
  width = newWidth; height = newHeight;
  ratio = Math.min(window.devicePixelRatio || 1, 1.5);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const type = region.projection.type;
  projectionClip = type === 'equalEarth' ? 180 : type === 'stereographic' ? 90 : 120;
  projectionRotation = d3.geoRotation(region.projection.rotate || [0, 0]);
  projection = (type === 'equalEarth' ? d3.geoEqualEarth() : type === 'stereographic' ? d3.geoStereographic() : d3.geoAzimuthalEqualArea())
    .rotate(region.projection.rotate || [0, 0]);
  if (projectionClip < 180) projection.clipAngle(projectionClip);
  projection.fitExtent([[22, 28], [width - 22, height - 48]], type === 'equalEarth' ? { type: 'Sphere' } : geography);
  path = d3.geoPath(projection, context);
  transform = d3.zoomIdentity;
  d3.select(canvas).call(zoom.transform, transform);
}
function updateInspector() {
  if (!raw) { $('inspector').textContent = selected ? 'No complete annual climate data is available at this location.' : ''; return; }
  if (!selected) {
    $('inspector').replaceChildren();
    return;
  }
  const cell = cellAt(...selected.coordinate);
  if (cell < 0 || raw[cell * 2] === -32768 || raw[cell * 2 + 1] === -32768) {
    $('inspector').textContent = `No ${region.label} land data at this location in ${currentYear}. Choose another point or year.`;
    return;
  }
  const [lon, lat] = coordinates(cell);
  const category = classes[cell];
  const title = document.createElement('h3');
  title.textContent = selected.name || `${latitudeText(lat, 2)}, ${longitudeText(lon, 2)}`;
  $('inspector').replaceChildren(title);
  const content = document.createElement('div');
  content.innerHTML = `<dl class="inspection-values"><div><dt>${currentYear} mean maximum temperature</dt><dd>${(raw[cell * 2] / 100).toFixed(1)} <span>°C</span></dd></div><div><dt>${currentYear} annual precipitation</dt><dd>${number.format(raw[cell * 2 + 1])} <span>mm</span></dd></div></dl><p class="inspection-note">Grid centre ${latitudeText(lat, 3)}, ${longitudeText(lon, 3)}. ${$('smoothing').checked ? 'Colour uses smoothed values; readings above are unsmoothed.' : 'Colour and readings use unsmoothed values.'}</p><p>${currentYear} mapped class: ${classText(category % 4, category >> 2)}. Fixed pooled annual ${region.label} baseline: ${baselineText()}.</p>`;
  $('inspector').append(content);
}
function inspect(coordinate, name = '') {
  selected = { coordinate, name };
  updateInspector();
  scheduleDraw();
}
function normalizeWinding(collection) {
  // GeoJSON uses counterclockwise exteriors; D3 spherical polygons use clockwise.
  for (const feature of collection.features) {
    const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
    for (const coordinates of polygons) {
      if (d3.geoArea({ type: 'Polygon', coordinates }) > 2 * Math.PI) coordinates.forEach(ring => ring.reverse());
    }
  }
  return collection;
}
async function fetchFile(url, type, signal) {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response[type]();
}
function decode(buffer, manifest, indices = false) {
  const cells = manifest.storedCells ?? manifest.width * manifest.height;
  if (buffer.byteLength !== cells * 4) throw new Error('Climate grid size does not match its metadata.');
  if (littleEndian) return indices ? new Uint32Array(buffer) : new Int16Array(buffer);
  const view = new DataView(buffer);
  const values = indices ? new Uint32Array(cells) : new Int16Array(cells * 2);
  for (let i = 0; i < values.length; i++) values[i] = indices ? view.getUint32(i * 4, true) : view.getInt16(i * 2, true);
  return values;
}
async function fetchGrid(file, manifest, signal, indices = false) {
  const response = await fetch(`data/${file}`, { signal });
  if (!response.ok) throw new Error(`${file}: HTTP ${response.status}`);
  // Fetch already removes HTTP Content-Encoding; otherwise decode the gzip asset.
  const encoded = response.headers.get('Content-Encoding')?.toLowerCase().includes('gzip');
  const stream = encoded ? response.body : response.body.pipeThrough(new DecompressionStream('gzip'), { signal });
  const buffer = await new Response(stream).arrayBuffer();
  signal.throwIfAborted();
  return decode(buffer, manifest, indices);
}
function trimCache(target = dataset, protectedYear = pendingYear) {
  let bytes = 0;
  for (const annual of target.cache.values()) bytes += annual.raw.byteLength + annual.smoothed.byteLength;
  for (const [year, annual] of target.cache) {
    if (bytes <= cacheBytesLimit) break;
    if ((target === dataset && year === currentYear) || year === protectedYear) continue;
    bytes -= annual.raw.byteLength + annual.smoothed.byteLength;
    target.cache.delete(year);
  }
}
function cancelOtherLoads(target, year) {
  if (!target) return;
  for (const [loadingYear, load] of target.loads) {
    if (loadingYear === year) continue;
    load.controller.abort();
    target.loads.delete(loadingYear);
  }
}
async function loadFrame(target, year, parentSignal) {
  if (target.cache.has(year)) {
    const cached = target.cache.get(year);
    target.cache.delete(year);
    target.cache.set(year, cached);
    return cached;
  }
  if (target.loads.has(year)) return target.loads.get(year).promise;
  const record = target.metadata.frames[String(year)];
  if (!record) throw new Error(`No annual frame is available for ${year}.`);
  const controller = new AbortController();
  const abort = () => controller.abort();
  parentSignal?.addEventListener('abort', abort, { once: true });
  if (parentSignal?.aborted) controller.abort();
  const load = { controller, promise: null };
  load.promise = (async () => {
    try {
      const [annualRaw, annualSmoothed, annualMetadata] = await Promise.all([
        fetchGrid(record.rawFile, target.metadata, controller.signal),
        fetchGrid(record.smoothedFile, target.metadata, controller.signal),
        fetchFile(`data/${record.metadataFile}`, 'json', controller.signal),
      ]);
      controller.signal.throwIfAborted();
      const result = { raw: annualRaw, smoothed: annualSmoothed, metadata: annualMetadata };
      target.cache.set(year, result);
      trimCache(target, year);
      return result;
    } catch (error) {
      controller.abort();
      throw error;
    } finally {
      parentSignal?.removeEventListener('abort', abort);
      if (target.loads.get(year) === load) target.loads.delete(year);
    }
  })();
  target.loads.set(year, load);
  return load.promise;
}
function prefetchNext() {
  // Large native regions retain only the committed pair; never speculative 100+ MB pairs.
  if (!raw || pendingRegion || raw.byteLength + smoothed.byteLength > cacheBytesLimit / 2) return;
  const next = metadata.years[metadata.years.indexOf(currentYear) + 1];
  if (next !== undefined) loadFrame(dataset, next).catch(() => {});
}
function setPlaying(value) {
  playing = value;
  clearTimeout(playbackTimer);
  playbackTimer = 0;
  $('play').textContent = playing ? 'Pause' : 'Play';
  $('play').setAttribute('aria-pressed', String(playing));
}
function syncTimeline() {
  const supported = Boolean(metadata?.years.length);
  const switching = pendingRegion !== null;
  const year = pendingYear ?? currentYear ?? requestedYear ?? metadata?.defaultYear;
  $('year-readout').value = supported ? pendingYear === null ? String(year) : `${year} · loading` : 'Unavailable';
  if (supported) $('year-slider').value = year;
  $('year-slider').setAttribute('aria-valuetext', supported ? pendingYear === null ? String(year) : `${year}, loading; displayed year ${currentYear ?? 'not yet available'}` : 'No annual coverage');
  for (const id of ['year-slider', 'play', 'speed', 'mode', 'classification', 'smoothing']) $(id).disabled = !supported || switching;
  const index = metadata?.years.indexOf(year) ?? -1;
  $('previous-year').disabled = !supported || switching || index <= 0;
  $('next-year').disabled = !supported || switching || index >= metadata.years.length - 1;
  $('download').disabled = switching || pendingYear !== null || currentYear === null;
  $('location-select').disabled = !dataset || switching || !cities.length;
  for (const id of ['borders', 'zoom-in', 'zoom-out', 'reset-view']) $(id).disabled = !dataset;
  $('region-select').disabled = !registry;
  canvas.setAttribute('aria-busy', String(switching || pendingYear !== null));
}
function updateYearLabels() {
  const available = currentYear !== null;
  for (const id of ['map-year', 'footer-year']) $(id).textContent = available ? currentYear : 'No coverage';
  document.title = `${region.title} bivariate climate map${available ? ` · ${currentYear}` : ' · coverage unavailable'}`;
  document.querySelector('meta[name="description"]').content = available
    ? `Explore ${region.label} annual temperature and precipitation in ${currentYear}, against fixed pooled ${baselineText()} TerraClimate thresholds.`
    : `${region.label} geographic borders. Complete annual TerraClimate coverage is unavailable.`;
  canvas.setAttribute('aria-label', available
    ? `${region.label} ${currentYear} temperature and precipitation map. Select a location on the map or use the city selector to inspect its climate values.`
    : `${region.label} borders. No complete annual climate raster is available.`);
  document.querySelector('.map-section').setAttribute('aria-label', `${region.label} bivariate climate map`);
  $('reset-view').title = `Reset to all of ${region.label}`;
  const footer = $('footer-year').parentElement;
  footer.firstChild.textContent = `${region.label} · `;
  $('data-summary').textContent = available
    ? `${number.format(currentMetadata.validCells)} land cells · ${currentYear} · 12 months · ${(metadata.dx * 60).toFixed(1)} arc-min grid${region.id === 'world' ? ' · approximately 28 km (stride 6)' : ' · native approximately 4 km'}`
    : metadata.coverageNote || region.coverageNote || 'No complete annual climate coverage is available.';
  $('region-coverage').textContent = available
    ? [metadata.coverageNote || region.coverageNote, metadata.annualMaskVariation, metadata.scope?.mask, 'Uncoloured land has no complete annual data; neutral borders show geographic context, not a climate class.'].filter(Boolean).join(' ')
    : $('data-summary').textContent;
}
function updateRegionLabels() {
  $('region-select').value = region.id;
  $('region-select').title = `${region.label} · displayed region`;
  $('region-select').dataset.scope = region.id;
  $('region-scope').textContent = metadata.scope?.selection || metadata.scope?.mask || region.coverageNote || `${region.label} land selected from Natural Earth country-continent polygons.`;
  const link = $('frame-index-link');
  link.href = `data/${region.metadataFile}`;
  link.textContent = `${region.label} annual frame index and fixed thresholds`;
  const method = $('region-method');
  const preparation = method.querySelector('a[href="scripts/build_data.py"]');
  const note = document.createTextNode(metadata.years.length
    ? `${metadata.years.length} annual frames at ${region.id === 'world' ? 'approximately 28 km, horizStride=6' : 'native approximately 4 km resolution'}. Fixed thresholds pool raw ${region.label} cell-year observations${metadata.pooledValidCellYears ? ` (${number.format(metadata.pooledValidCellYears)})` : ''}. See the `
    : 'No complete annual frames are available. See the ');
  method.replaceChildren(note, link, document.createTextNode('. '), ...(preparation ? [preparation] : []));
  const selector = $('location-select');
  selector.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = ''; placeholder.textContent = 'Choose a city…'; selector.append(placeholder);
  cities = region.cities || [];
  cities.forEach(([name], i) => {
    const option = document.createElement('option'); option.value = i; option.textContent = name; selector.append(option);
  });
  $('year-start').textContent = metadata.years[0] ?? '—';
  $('year-end').textContent = metadata.years.at(-1) ?? '—';
  if (metadata.years.length) {
    $('year-slider').min = metadata.years[0];
    $('year-slider').max = metadata.years.at(-1);
  }
}
async function seekRegion(id) {
  const choice = registry.regions.find(item => item.id === id);
  if (!choice) return;
  $('region-select').value = id;
  setPlaying(false);
  cancelRipple();
  const token = ++requestToken;
  regionController?.abort();
  cancelOtherLoads(dataset);
  if (dataset) for (const year of dataset.cache.keys()) if (year !== currentYear) dataset.cache.delete(year);
  const controller = new AbortController();
  regionController = controller;
  pendingRegion = id;
  pendingYear = null;
  $('error').hidden = true;
  $('timeline-status').textContent = `Loading ${choice.label}…${region ? ` Still displaying ${region.label}${currentYear === null ? ' borders' : ` ${currentYear}`}.` : ''}`;
  syncTimeline();
  try {
    const manifest = await fetchFile(`data/${choice.metadataFile}`, 'json', controller.signal);
    const [borders, indices] = await Promise.all([
      fetchFile(`data/${manifest.borderFile}`, 'json', controller.signal),
      manifest.cellIndexFile ? fetchGrid(manifest.cellIndexFile, manifest, controller.signal, true) : null,
    ]);
    const staged = { metadata: manifest, geography: normalizeWinding(borders), indices, cache: new Map(), loads: new Map() };
    const year = manifest.years.includes(currentYear) ? currentYear : manifest.defaultYear;
    const annual = manifest.years.length ? await loadFrame(staged, year, controller.signal) : null;
    controller.signal.throwIfAborted();
    if (token !== requestToken) return;
    dataset?.cache.clear();
    dataset = staged; region = choice; metadata = manifest; geography = staged.geography; cellIndices = indices;
    raw = annual?.raw ?? null; smoothed = annual?.smoothed ?? null;
    currentMetadata = annual?.metadata ?? null; currentYear = annual ? year : null;
    requestedYear = currentYear; classes = null; selected = null; highlighted = null;
    pendingRegion = null; regionController = null;
    projection = null;
    pixelCells = null; image = null; raster.width = 0;
    updateRegionLabels();
    updateYearLabels();
    resize();
    if (raw) updateClasses();
    else { updateLegend(); updateInspector(); scheduleDraw(true); }
    flushDraw();
    $('loading').hidden = true;
    $('timeline-status').textContent = raw ? '' : $('data-summary').textContent;
    syncTimeline();
    trimCache();
    prefetchNext();
  } catch (error) {
    if (token !== requestToken) return;
    controller.abort();
    pendingRegion = null; regionController = null; requestedYear = currentYear;
    $('loading').hidden = true;
    if (region) $('region-select').value = region.id;
    const message = `Could not load ${choice.label}: ${error.message}. ${region ? `Still displaying ${region.label}${currentYear === null ? ' borders' : ` ${currentYear}`}. Select the region again to retry.` : 'Choose a region to retry. Serve this folder over HTTP.'}`;
    $('timeline-status').textContent = message;
    if (!dataset) { $('error').hidden = false; $('error').textContent = message; }
    syncTimeline();
  }
}
function schedulePlayback() {
  clearTimeout(playbackTimer);
  playbackTimer = 0;
  if (!playing || pendingRegion !== null || pendingYear !== null || currentYear === null) return;
  const index = metadata.years.indexOf(currentYear);
  if (index === metadata.years.length - 1) {
    setPlaying(false);
    $('timeline-status').textContent = 'Animation complete.';
    return;
  }
  playbackTimer = setTimeout(() => {
    playbackTimer = 0;
    if (playing) seekYear(metadata.years[index + 1], true);
  }, Number($('speed').value));
}
async function seekYear(year, fromPlayback = false) {
  if (!fromPlayback) setPlaying(false);
  if (!metadata || pendingRegion !== null || !metadata.years.includes(year)) return;
  clearTimeout(playbackTimer);
  playbackTimer = 0;
  const token = ++requestToken;
  requestedYear = year;
  pendingYear = year;
  cancelOtherLoads(dataset, year);
  $('error').hidden = true;
  $('timeline-status').textContent = `Loading ${year}…${currentYear === null ? '' : ` Displaying ${currentYear}.`}`;
  syncTimeline();
  try {
    const target = dataset;
    const annual = await loadFrame(target, year);
    if (token !== requestToken || target !== dataset) return;
    raw = annual.raw;
    smoothed = annual.smoothed;
    currentMetadata = annual.metadata;
    currentYear = year;
    updateYearLabels();
    updateClasses();
    flushDraw();
    pendingYear = null;
    $('loading').hidden = true;
    $('timeline-status').textContent = '';
    syncTimeline();
    trimCache();
    schedulePlayback();
    prefetchNext();
  } catch (error) {
    if (token !== requestToken) return;
    pendingYear = null;
    setPlaying(false);
    $('loading').hidden = true;
    $('timeline-status').textContent = `Could not load ${year}: ${error.message}. ${currentYear === null ? 'No year displayed.' : `Still displaying ${currentYear}.`} Seek ${year} or press Play to retry.`;
    syncTimeline();
  }
}
function togglePlayback() {
  if (!metadata?.years.length || pendingRegion !== null) return;
  if (playing) {
    setPlaying(false);
    $('timeline-status').textContent = pendingYear === null ? '' : `Loading ${pendingYear}… playback paused.`;
    return;
  }
  setPlaying(true);
  if (pendingYear !== null) return;
  if (requestedYear !== currentYear) {
    seekYear(requestedYear, true);
  } else if (currentYear === metadata.years.at(-1)) {
    seekYear(metadata.years[0], true);
  } else {
    $('timeline-status').textContent = '';
    schedulePlayback();
  }
}
function downloadMap() {
  if (pendingRegion !== null || pendingYear !== null || currentYear === null) return;
  flushDraw();
  const exportYear = currentYear, exportRegion = region.id;
  // Include the legend, active method and credits in the file, not just the map.
  const out = document.createElement('canvas');
  const outputWidth = Math.max(1100, canvas.width);
  const mapHeight = Math.round(canvas.height * outputWidth / canvas.width);
  out.width = outputWidth;
  out.height = mapHeight + 350;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#fffdf9'; ctx.fillRect(0, 0, out.width, out.height);
  ctx.fillStyle = '#111'; ctx.font = '28px sans-serif';
  ctx.fillText(`${region.label} · ${$('mode').selectedOptions[0].textContent} · ${exportYear}`, 28, 43);
  ctx.drawImage(canvas, 0, 62, outputWidth, mapHeight);
  const mode = $('mode').value;
  const rgb = colors(mode);
  const size = 27, left = 50, top = mapHeight + 90;
  const showT = mode !== 'precipitation', showP = mode !== 'temperature';
  for (let p = showP ? 3 : 0; p >= 0; p--) for (let t = 0; t < (showT ? 4 : 1); t++) {
    ctx.fillStyle = rgb[p * 4 + t].formatHex();
    ctx.fillRect(left + t * size, top + (showP ? 3 - p : 0) * size, showT ? size : size * 4, showP ? size : size * 4);
  }
  ctx.fillStyle = '#28303d'; ctx.font = '12px sans-serif';
  if (showT) ctx.fillText('Temperature →', left, top + 127);
  if (showP) {
    ctx.save(); ctx.translate(left - 10, top + 108); ctx.rotate(-Math.PI / 2);
    ctx.fillText('Precipitation →', 0, 0); ctx.restore();
  }
  const lines = [
    `${$('mode').selectedOptions[0].textContent} · ${$('classification').selectedOptions[0].textContent} · ${$('smoothing').checked ? `Gaussian σ=${metadata.smoothing.sigmaCells} selected-grid cells` : 'Unsmoothed'}`,
    `Temperature breaks (°C): ${breaksT.map(v => v.toFixed(1)).join(' / ')}`,
    `Precipitation breaks (mm/year): ${breaksP.map(v => Math.round(v)).join(' / ')}`,
    `Fixed thresholds: pooled raw annual ${region.label} land cells, ${baselineText()}.`,
    `Data: TerraClimate v1.1 · ${exportYear}, 12 months (Abatzoglou et al., 2026; CC0) · Borders: Natural Earth`,
    'Adapted from Milan Janosov; earlier maps by Muhammad Mohsin Raza and Philippe Rivière.',
    'Method: https://milanjanosov.substack.com/p/bivariate-climate-map-temperature',
    `Data: https://www.climatologylab.org/terraclimate.html · ${projectionName()}`,
    `Frame index: ${new URL(`data/${region.metadataFile}`, document.baseURI).href}`,
    `${region.id === 'world' ? 'World · approximately 28 km (horizStride=6)' : `${region.label} · native approximately 4 km`} · one year, not a climate normal.`,
    highlighted === null ? 'Natural Earth country-continent selection; incomplete cells are not mapped.' : `Selected range highlighted: ${$('legend-detail').textContent}`,
  ];
  lines.forEach((line, i) => ctx.fillText(line, 190, top + 10 + i * 22, out.width - 218));
  out.toBlob(blob => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = `${exportRegion}-climate-${exportYear}.png`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, 'image/png');
}
async function start() {
  const controls = [...document.querySelectorAll('button, select, input')];
  controls.forEach(control => { control.disabled = true; });
  const blogPost = $('project-blog-link');
  const publication = $('project-blog-date');
  if (blogPost && publication && Date.now() >= Date.parse(publication.querySelector('time').dateTime)) {
    blogPost.hidden = false;
    publication.hidden = true;
  }
  try {
    zoom = d3.zoom().scaleExtent([1, 8]).touchable(true)
      .on('zoom', event => {
        transform = event.transform;
        scheduleDraw(true);
      });
    d3.select(canvas).call(zoom).on('dblclick.zoom', null);
    canvas.addEventListener('click', event => {
      if (event.defaultPrevented || !projection) return;
      cancelRipple();
      const rect = canvas.getBoundingClientRect();
      const coordinate = inverseCoordinate(transform.invert([event.clientX - rect.left, event.clientY - rect.top]));
      if (!coordinate) return;
      inspect(coordinate);
      $('location-select').value = '';
    });
    $('location-select').addEventListener('change', event => {
      cancelRipple();
      if (event.target.value === '') { selected = null; updateInspector(); scheduleDraw(); return; }
      const [name, lon, lat] = cities[Number(event.target.value)];
      inspect([lon, lat], name);
      startRipple();
    });
    for (const id of ['mode', 'classification', 'smoothing']) $(id).addEventListener('change', () => { highlighted = null; updateClasses(); });
    $('borders').addEventListener('change', scheduleDraw);
    $('zoom-in').addEventListener('click', () => d3.select(canvas).call(zoom.scaleBy, 1.5));
    $('zoom-out').addEventListener('click', () => d3.select(canvas).call(zoom.scaleBy, 1 / 1.5));
    $('reset-view').addEventListener('click', () => d3.select(canvas).call(zoom.transform, d3.zoomIdentity));
    $('download').addEventListener('click', downloadMap);
    $('region-select').addEventListener('change', event => seekRegion(event.target.value));
    $('year-slider').addEventListener('input', event => seekYear(Number(event.target.value)));
    for (const [id, delta] of [['previous-year', -1], ['next-year', 1]]) {
      $(id).addEventListener('click', () => {
        if (!metadata || pendingRegion !== null) return;
        const index = metadata.years.indexOf(pendingYear ?? currentYear ?? requestedYear);
        const year = metadata.years[index + delta];
        if (year !== undefined) seekYear(year);
      });
    }
    $('play').addEventListener('click', togglePlayback);
    $('speed').addEventListener('change', schedulePlayback);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden || !playing) return;
      setPlaying(false);
      $('timeline-status').textContent = pendingYear === null ? 'Playback paused while page is hidden.' : `Loading ${pendingYear}… playback paused while page is hidden.`;
    });
    setPlaying(false);
    new ResizeObserver(resize).observe($('map-wrap'));
    registry = await fetchFile('data/regions/index.json', 'json');
    $('region-select').replaceChildren();
    registry.regions.forEach(item => {
      const option = document.createElement('option'); option.value = item.id; option.textContent = item.title;
      $('region-select').append(option);
    });
    $('region-select').disabled = false;
    await seekRegion(registry.defaultRegion);
  } catch (error) {
    $('loading').hidden = true;
    $('error').hidden = false;
    $('error').textContent = `Could not load the climate map. ${error.message} Serve this folder over HTTP and reload.`;
    console.error(error);
  }
}
start();
