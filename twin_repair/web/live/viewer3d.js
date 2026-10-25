// Presentation viewer for scene0/scene1 SceneSpec graphs.
// Honest mirror of the canonical geometry in blackbox/static3d/scene.js: same rack centers,
// dimensions, busbar positions and cable waypoints. It is not the proof image; the fixed-pose
// PNGs that Argon inspected are shown in the evidence drawer.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

const RACK_IDS = ['N1', 'N2', 'N3', 'N4'];
const DEFAULT_RACK_X = { N1: 0.0, N2: 0.82, N3: 1.64, N4: 2.46 };
const RACK_W = 0.6, RACK_H = 2.0, RACK_D = 1.0;
const FEEDER = {
  F1: { color: 0x1d8aff, y: 2.38, z: 0.26 },
  F2: { color: 0xff8c1a, y: 2.12, z: 0.54 },
};
const BUS_X = 1.23, BUS_LEN = 3.25;
const HOT_CONNECTOR = new THREE.Vector3(2.57, 1.2, 0.555);
const TARGET = new THREE.Vector3(1.1, 1.36, 0.45);
const VIEW_DIR = new THREE.Vector3(0.16, 0.13, 1).normalize();

// Same endpoints as the canonical scene (rear PDU inlet -> feeder busbar); the presentation
// route stays outside the rear door so the whole drop is visible instead of passing through the cabinet.
function waypoints(cx, feeder) {
  if (feeder === 'F1') {
    return [[cx + 0.11, 1.38, 0.55], [cx + 0.03, 1.72, 0.6], [cx - 0.07, 2.08, 0.52], [cx - 0.12, 2.3, 0.34], [cx - 0.14, 2.38, 0.26]];
  }
  return [[cx + 0.11, 1.38, 0.55], [cx + 0.17, 1.62, 0.6], [cx + 0.22, 1.9, 0.6], [cx + 0.24, 2.06, 0.56], [cx + 0.24, 2.12, 0.54]];
}

function canvasTexture(w, h, draw, srgb = true) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  const t = new THREE.CanvasTexture(c);
  if (srgb) t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

function perforatedDoorTextures(lowSpec) {
  const s = lowSpec ? 256 : 512;
  const draw = (bump) => (g, w, h) => {
    g.fillStyle = bump ? '#fff' : '#2a2f36';
    g.fillRect(0, 0, w, h);
    const step = w / 22;
    for (let y = step; y < h - step * 0.5; y += step * 0.86) {
      const row = Math.round(y / (step * 0.86));
      for (let x = step * (row % 2 ? 1 : 0.5); x < w - step * 0.4; x += step) {
        g.beginPath();
        g.arc(x, y, step * 0.3, 0, Math.PI * 2);
        g.fillStyle = bump ? '#000' : '#07090c';
        g.fill();
      }
    }
    g.lineWidth = step * 0.9;
    g.strokeStyle = bump ? '#fff' : '#30363e';
    g.strokeRect(0, 0, w, h);
  };
  return { map: canvasTexture(s, s * 3, draw(false)), bump: canvasTexture(s, s * 3, draw(true), false) };
}

function labelTexture(text, { bg = 'rgba(12,16,22,0.88)', fg = '#f4f7fb', accent = null, w = 0, h = 112, font = 64 } = {}) {
  const fontSpec = `700 ${font}px "Inter", "Segoe UI", system-ui, sans-serif`;
  if (!w) {
    const m = document.createElement('canvas').getContext('2d');
    m.font = fontSpec;
    w = Math.ceil(m.measureText(text).width + font * 0.9 + (accent ? 28 : 0));
  }
  const tex = canvasTexture(w, h, (g) => {
    g.clearRect(0, 0, w, h);
    g.fillStyle = bg;
    roundRect(g, 4, 4, w - 8, h - 8, 18);
    g.fill();
    if (accent) {
      g.save();
      roundRect(g, 4, 4, w - 8, h - 8, 18);
      g.clip();
      g.fillStyle = accent;
      g.fillRect(4, 4, 20, h - 8);
      g.restore();
    }
    g.fillStyle = fg;
    g.font = fontSpec;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(text, w / 2 + (accent ? 12 : 0), h / 2 + 2);
  });
  tex.userData.aspect = w / h;
  return tex;
}

function floorTexture(lowSpec) {
  const s = lowSpec ? 256 : 512;
  return canvasTexture(s, s, (g, w, h) => {
    g.fillStyle = '#585f68';
    g.fillRect(0, 0, w, h);
    const img = g.getImageData(0, 0, w, h);
    for (let i = 0; i < img.data.length; i += 4) {
      const n = (Math.random() - 0.5) * 14;
      img.data[i] += n; img.data[i + 1] += n; img.data[i + 2] += n;
    }
    g.putImageData(img, 0, 0);
    const lw = Math.max(2, w / 128);
    g.lineWidth = lw;
    g.strokeStyle = '#262a30';
    g.strokeRect(0, 0, w, h);
    g.strokeStyle = 'rgba(255,255,255,0.08)';
    g.strokeRect(lw, lw, w - 2 * lw, h - 2 * lw);
  });
}

export function webglAvailable() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

export function createViewer(container, { onContextLost } = {}) {
  if (!webglAvailable()) throw new Error('WebGL unavailable');
  const lowSpec = window.matchMedia('(pointer: coarse)').matches || Math.min(window.innerWidth, window.innerHeight) < 700;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, lowSpec ? 1.25 : 1.75));
  renderer.domElement.setAttribute('role', 'img');
  renderer.domElement.setAttribute('aria-label', '3D rear-aisle view: racks N1 to N4, feeder F1 in blue, feeder F2 in orange');
  container.appendChild(renderer.domElement);

  const disposables = [];
  const track = (o) => { disposables.push(o); return o; };

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0e13);
  scene.fog = new THREE.Fog(0x0b0e13, 7, 15);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const envTex = track(pmrem.fromScene(new RoomEnvironment(), 0.04).texture);
  pmrem.dispose();
  scene.environment = envTex;
  scene.environmentIntensity = 0.42;

  const camera = new THREE.PerspectiveCamera(36, 1, 0.05, 40);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(TARGET);
  controls.enableDamping = !reducedMotion;
  controls.dampingFactor = 0.09;
  controls.enablePan = false;
  controls.minPolarAngle = 0.95;
  controls.maxPolarAngle = 1.62;
  controls.minAzimuthAngle = -0.85;
  controls.maxAzimuthAngle = 0.95;
  controls.minDistance = 1.4;
  controls.rotateSpeed = 0.6;

  // indoor lighting: soft hemisphere + one overhead shadow caster + cool fill
  scene.add(new THREE.HemisphereLight(0xdfe8f5, 0x20242a, 0.55));
  const key = new THREE.DirectionalLight(0xf3f6ff, 1.9);
  key.position.set(1.6, 4.6, 3.2);
  key.target.position.set(1.23, 0.8, 0.0);
  key.castShadow = true;
  key.shadow.mapSize.set(lowSpec ? 1024 : 2048, lowSpec ? 1024 : 2048);
  Object.assign(key.shadow.camera, { left: -2.6, right: 2.6, top: 2.8, bottom: -1.2, near: 1, far: 10 });
  key.shadow.bias = -0.0004;
  key.shadow.normalBias = 0.02;
  key.shadow.radius = 4;
  scene.add(key, key.target);
  const fill = new THREE.DirectionalLight(0x9fb4d6, 0.35);
  fill.position.set(-3, 2, 4);
  scene.add(fill);

  // room shell
  const floorTex = track(floorTexture(lowSpec));
  floorTex.wrapS = floorTex.wrapT = THREE.RepeatWrapping;
  floorTex.repeat.set(16 / 0.6, 12 / 0.6);
  const floor = new THREE.Mesh(track(new THREE.PlaneGeometry(16, 12)), track(new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.78, metalness: 0.05 })));
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(1.23, 0, 1);
  floor.receiveShadow = true;
  scene.add(floor);
  const wall = new THREE.Mesh(track(new THREE.PlaneGeometry(16, 4)), track(new THREE.MeshStandardMaterial({ color: 0x1a1f26, roughness: 0.92 })));
  wall.position.set(1.23, 2, -2.2);
  scene.add(wall);
  const ledPanelMat = track(new THREE.MeshStandardMaterial({ color: 0x000000, emissive: 0xeaf2ff, emissiveIntensity: 1.6 }));
  const ledPanelGeo = track(new THREE.PlaneGeometry(0.58, 0.58));
  for (let i = -1; i < 5; i++) {
    for (const z of [1.6, -0.9]) {
      const p = new THREE.Mesh(ledPanelGeo, ledPanelMat);
      p.rotation.x = Math.PI / 2;
      p.position.set(i * 1.2, 3.25, z);
      scene.add(p);
    }
  }

  // racks
  const door = perforatedDoorTextures(lowSpec);
  track(door.map); track(door.bump);
  const steelMat = track(new THREE.MeshStandardMaterial({ color: 0x23272d, metalness: 0.55, roughness: 0.48 }));
  const doorMat = track(new THREE.MeshStandardMaterial({ map: door.map, bumpMap: door.bump, bumpScale: 1.2, metalness: 0.5, roughness: 0.55 }));
  const handleMat = track(new THREE.MeshPhysicalMaterial({ color: 0xb8bec6, metalness: 0.95, roughness: 0.22, clearcoat: 0.6 }));
  const darkMat = track(new THREE.MeshStandardMaterial({ color: 0x0f1216, roughness: 0.7 }));
  const pduMat = track(new THREE.MeshStandardMaterial({ color: 0x3a4048, metalness: 0.4, roughness: 0.5 }));
  const trayMat = track(new THREE.MeshStandardMaterial({ color: 0x8a9099, metalness: 0.8, roughness: 0.35 }));
  const ledGreen = track(new THREE.MeshStandardMaterial({ color: 0x000000, emissive: 0x30ff7a, emissiveIntensity: 2.2 }));
  const ledAmber = track(new THREE.MeshStandardMaterial({ color: 0x000000, emissive: 0xffa21a, emissiveIntensity: 2.6 }));

  const rackBodyGeo = track(new RoundedBoxGeometry(RACK_W, RACK_H, RACK_D, 3, 0.014));
  const doorGeo = track(new THREE.PlaneGeometry(RACK_W - 0.06, RACK_H - 0.16));
  const handleGeo = track(new THREE.BoxGeometry(0.018, 0.22, 0.03));
  const hingeGeo = track(new THREE.CylinderGeometry(0.009, 0.009, 0.08, 10));
  const plinthGeo = track(new THREE.BoxGeometry(RACK_W, 0.06, RACK_D - 0.02));
  const pduGeo = track(new THREE.BoxGeometry(0.1, 0.07, 0.05));
  const ledGeo = track(new THREE.BoxGeometry(0.03, 0.008, 0.004));
  const plateGeo = track(new THREE.PlaneGeometry(0.2, 0.117));
  const rackLeds = {};
  for (const rk of RACK_IDS) {
    const g = new THREE.Group();
    g.position.x = DEFAULT_RACK_X[rk];
    const body = new THREE.Mesh(rackBodyGeo, steelMat);
    body.position.y = RACK_H / 2;
    body.castShadow = body.receiveShadow = true;
    const plinth = new THREE.Mesh(plinthGeo, darkMat);
    plinth.position.y = 0.03;
    const d = new THREE.Mesh(doorGeo, doorMat);
    d.position.set(0, RACK_H / 2 - 0.02, RACK_D / 2 + 0.004);
    d.receiveShadow = true;
    const handle = new THREE.Mesh(handleGeo, handleMat);
    handle.position.set(-RACK_W / 2 + 0.07, 1.05, RACK_D / 2 + 0.02);
    g.add(body, plinth, d, handle);
    for (const hy of [0.35, 1.0, 1.65]) {
      const h = new THREE.Mesh(hingeGeo, handleMat);
      h.position.set(RACK_W / 2 - 0.035, hy, RACK_D / 2 + 0.01);
      g.add(h);
    }
    const pdu = new THREE.Mesh(pduGeo, pduMat);
    pdu.position.set(0.11, 1.38, 0.515);
    pdu.castShadow = true;
    g.add(pdu);
    rackLeds[rk] = [];
    for (let i = 0; i < 3; i++) {
      const l = new THREE.Mesh(ledGeo, ledGreen);
      l.position.set(-0.16 + i * 0.045, 1.9, RACK_D / 2 + 0.008);
      g.add(l);
      rackLeds[rk].push(l);
    }
    const tex = track(labelTexture(rk, { w: 192, h: 112, font: 70 }));
    const plate = new THREE.Mesh(plateGeo, track(new THREE.MeshBasicMaterial({ map: tex, transparent: true, toneMapped: false })));
    plate.position.set(-0.08, 1.74, RACK_D / 2 + 0.012);
    const sp = new THREE.Sprite(track(new THREE.SpriteMaterial({ map: tex, depthTest: false, toneMapped: false })));
    sp.scale.set(0.24, 0.14, 1);
    sp.position.set(0, 2.66, 0.2);
    sp.renderOrder = 10;
    g.add(plate, sp);
    scene.add(g);
  }

  // feeders: busbar inside a suspended ladder tray
  const rodGeo = track(new THREE.CylinderGeometry(0.006, 0.006, 1, 8));
  const rungGeo = track(new THREE.BoxGeometry(0.012, 0.01, 0.15));
  const railGeo = track(new THREE.BoxGeometry(BUS_LEN + 0.1, 0.035, 0.008));
  const busGeo = track(new THREE.BoxGeometry(BUS_LEN, 0.06, 0.06));
  const feederObjs = {};
  for (const [fid, f] of Object.entries(FEEDER)) {
    const busMat = track(new THREE.MeshStandardMaterial({ color: f.color, roughness: 0.62, metalness: 0.15 }));
    const bus = new THREE.Mesh(busGeo, busMat);
    bus.position.set(BUS_X, f.y, f.z);
    bus.castShadow = true;
    scene.add(bus);
    for (const dz of [-0.07, 0.07]) {
      const rail = new THREE.Mesh(railGeo, trayMat);
      rail.position.set(BUS_X, f.y - 0.02, f.z + dz);
      scene.add(rail);
    }
    for (let x = BUS_X - BUS_LEN / 2; x <= BUS_X + BUS_LEN / 2 + 1e-6; x += 0.25) {
      const rung = new THREE.Mesh(rungGeo, trayMat);
      rung.position.set(x, f.y - 0.04, f.z);
      scene.add(rung);
    }
    for (const x of [BUS_X - 1.5, BUS_X - 0.5, BUS_X + 0.5, BUS_X + 1.5]) {
      for (const dz of [-0.07, 0.07]) {
        const rod = new THREE.Mesh(rodGeo, trayMat);
        rod.scale.y = 3.25 - f.y;
        rod.position.set(x, (3.25 + f.y) / 2, f.z + dz);
        scene.add(rod);
      }
    }
    const labelSprite = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false, toneMapped: false }));
    labelSprite.position.set(BUS_X - BUS_LEN / 2 - 0.06, f.y, f.z);
    labelSprite.center.set(1, 0.5);
    labelSprite.renderOrder = 11;
    scene.add(labelSprite);
    feederObjs[fid] = { busMat, labelSprite, labelTex: null };
  }

  function setFeederLabel(fid, text, overload) {
    const o = feederObjs[fid];
    if (o.labelTex) o.labelTex.dispose();
    const accent = '#' + FEEDER[fid].color.toString(16).padStart(6, '0');
    o.labelTex = labelTexture(text, { h: 120, font: 60, accent, bg: overload ? 'rgba(168,20,32,0.94)' : 'rgba(12,16,22,0.88)' });
    o.labelSprite.material.map = o.labelTex;
    o.labelSprite.material.needsUpdate = true;
    o.labelSprite.scale.set(0.13 * o.labelTex.userData.aspect, 0.13, 1);
    o.busMat.emissive.setHex(overload ? 0x9a0c18 : 0x000000);
    o.busMat.emissiveIntensity = overload ? 1.0 : 0;
  }

  // anchored N4 rear-connector heat cue (no whole-rack glow)
  const hotHousing = new THREE.Mesh(track(new THREE.BoxGeometry(0.07, 0.05, 0.03)), pduMat);
  hotHousing.position.copy(HOT_CONNECTOR);
  scene.add(hotHousing);
  const glowTex = track(canvasTexture(128, 128, (g, w, h) => {
    const grd = g.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, w / 2);
    grd.addColorStop(0, 'rgba(255,240,200,1)');
    grd.addColorStop(0.25, 'rgba(255,120,30,0.85)');
    grd.addColorStop(1, 'rgba(255,40,0,0)');
    g.fillStyle = grd;
    g.fillRect(0, 0, w, h);
  }));
  const glow = new THREE.Sprite(track(new THREE.SpriteMaterial({ map: glowTex, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false })));
  glow.scale.set(0.2, 0.2, 1);
  glow.position.copy(HOT_CONNECTOR).add(new THREE.Vector3(0, 0, 0.02));
  const hotLight = new THREE.PointLight(0xff6a1a, 0.6, 0.9, 2);
  hotLight.position.copy(HOT_CONNECTOR).add(new THREE.Vector3(0, 0, 0.08));
  const hotLabel = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false, toneMapped: false }));
  hotLabel.position.copy(HOT_CONNECTOR).add(new THREE.Vector3(0.2, -0.2, 0.1));
  hotLabel.center.set(1, 0.5);
  hotLabel.renderOrder = 12;
  scene.add(glow, hotLight, hotLabel);
  let hotLabelTex = null;

  // cables: the four graph edges being evaluated
  const cables = {};
  const clampGeo = track(new THREE.TorusGeometry(0.036, 0.008, 8, 20));
  const lugGeo = track(new THREE.BoxGeometry(0.06, 0.05, 0.05));
  let edges = {};

  function cableGeometry(points) {
    const curve = new THREE.CatmullRomCurve3(points.map((p) => new THREE.Vector3(...p)), false, 'centripetal');
    return new THREE.TubeGeometry(curve, lowSpec ? 40 : 72, 0.03, lowSpec ? 10 : 14, false);
  }

  function upsertCable(rk, feeder, t = 1, from = null) {
    const cx = DEFAULT_RACK_X[rk];
    const target = waypoints(cx, feeder);
    const start = from ? waypoints(cx, from) : target;
    const pts = target.map((p, i) => p.map((v, k) => start[i][k] + (v - start[i][k]) * t));
    let c = cables[rk];
    if (!c) {
      const mat = new THREE.MeshStandardMaterial({ color: FEEDER[feeder].color, roughness: 0.66, metalness: 0.05 });
      const mesh = new THREE.Mesh(cableGeometry(pts), mat);
      mesh.castShadow = true;
      const clamp = new THREE.Mesh(clampGeo, trayMat);
      clamp.rotation.x = Math.PI / 2;
      const lug = new THREE.Mesh(lugGeo, pduMat);
      scene.add(mesh, clamp, lug);
      c = cables[rk] = { mesh, mat, clamp, lug };
    } else {
      c.mesh.geometry.dispose();
      c.mesh.geometry = cableGeometry(pts);
    }
    c.mat.color.setHex(FEEDER[from || feeder].color).lerp(new THREE.Color(FEEDER[feeder].color), t);
    const end = pts[pts.length - 1];
    c.clamp.position.set(end[0], end[1] - 0.06, end[2]);
    c.lug.position.set(end[0], end[1], end[2]);
  }

  // render on demand
  let needsRender = true;
  let rafId = 0;
  let disposed = false;
  const animations = new Set();
  function requestRender() {
    needsRender = true;
    if (!rafId && !disposed) rafId = requestAnimationFrame(loop);
  }
  function loop(now) {
    rafId = 0;
    if (disposed) return;
    for (const a of [...animations]) {
      const t = Math.min(1, (now - a.t0) / a.duration);
      a.step(t);
      if (t >= 1) { animations.delete(a); if (a.done) a.done(); }
    }
    const moving = controls.update();
    const busy = animations.size > 0 || moving;
    if (needsRender || busy) {
      renderer.render(scene, camera);
      needsRender = false;
    }
    if (busy) rafId = requestAnimationFrame(loop);
  }
  controls.addEventListener('change', requestRender);

  function setHeat(tempC, caption = 'N4 connector') {
    if (hotLabelTex) hotLabelTex.dispose();
    const ok = Number.isFinite(tempC);
    const text = ok ? `${caption} ${tempC.toFixed(1)} °C` : caption;
    hotLabelTex = labelTexture(text, { h: 120, font: 56, accent: '#ff5a1f', bg: 'rgba(30,10,6,0.92)' });
    hotLabel.material.map = hotLabelTex;
    hotLabel.material.needsUpdate = true;
    hotLabel.scale.set(0.12 * hotLabelTex.userData.aspect, 0.12, 1);
    const hot = ok && tempC >= 75;
    glow.visible = hot;
    hotLight.intensity = hot ? 0.6 : 0;
    for (const l of rackLeds.N4) l.material = hot ? ledAmber : ledGreen;
    requestRender();
  }

  function setHighlight(racks) {
    const set = new Set(racks || []);
    for (const rk of RACK_IDS) {
      const c = cables[rk];
      if (!c) continue;
      c.mat.emissive.copy(c.mat.color);
      c.mat.emissiveIntensity = set.has(rk) ? 0.55 : 0;
    }
    requestRender();
  }

  function setLoads(loads) {
    for (const fid of ['F1', 'F2']) {
      const l = loads && loads[fid];
      if (l && Number.isFinite(l.current) && Number.isFinite(l.limit)) {
        setFeederLabel(fid, l.current > l.limit ? `${fid} OVER` : fid, l.current > l.limit);
      } else {
        setFeederLabel(fid, fid, false);
      }
    }
    requestRender();
  }

  // framing: reframe by distance (not FOV) so cables stay legible on portrait phones
  function frame() {
    const w = Math.max(1, container.clientWidth);
    const h = Math.max(1, container.clientHeight);
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    const halfW = 1.95, halfH = 1.45;
    const vt = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const dist = Math.max(halfH / vt, halfW / (vt * camera.aspect)) + 0.5;
    camera.position.copy(TARGET).addScaledVector(VIEW_DIR, dist);
    camera.far = dist + 20;
    camera.updateProjectionMatrix();
    controls.target.copy(TARGET);
    controls.maxDistance = Math.max(9, dist * 1.5);
    controls.update();
    requestRender();
  }
  const ro = new ResizeObserver(frame);
  ro.observe(container);

  const onLost = (e) => {
    e.preventDefault();
    if (onContextLost) onContextLost();
  };
  renderer.domElement.addEventListener('webglcontextlost', onLost);

  setLoads(null);
  frame();

  return {
    // Draw the graph from a SceneSpec. Cables only move when `animateFrom` is a previous graph
    // and the caller has a validated patch; otherwise the graph is placed as-is.
    setScene(spec, { animateFrom = null, highlight = [] } = {}) {
      const next = {};
      for (const e of (spec && spec.edges) || []) next[e.rack_id] = e.feeder_id;
      const prev = animateFrom || {};
      const changed = RACK_IDS.filter((rk) => prev[rk] && next[rk] && prev[rk] !== next[rk]);
      edges = next;
      for (const a of [...animations]) animations.delete(a);
      if (changed.length && !reducedMotion) {
        for (const rk of RACK_IDS) {
          if (!next[rk]) continue;
          if (changed.includes(rk)) upsertCable(rk, next[rk], 0, prev[rk]);
          else upsertCable(rk, next[rk]);
        }
        setHighlight([]);
        animations.add({
          t0: performance.now() + 250,
          duration: 1700,
          step: (t) => {
            const k = Math.max(0, t);
            const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
            for (const rk of changed) upsertCable(rk, next[rk], e, prev[rk]);
          },
          done: () => setHighlight(highlight.length ? highlight : changed),
        });
      } else {
        for (const rk of RACK_IDS) if (next[rk]) upsertCable(rk, next[rk]);
        setHighlight(highlight.length ? highlight : changed);
      }
      if (spec && spec.hotspot && Number.isFinite(spec.hotspot.temperature_c)) setHeat(spec.hotspot.temperature_c);
      requestRender();
      return changed;
    },
    setHeat,
    setHighlight,
    setLoads,
    resetView: frame,
    edges: () => ({ ...edges }),
    dispose() {
      disposed = true;
      if (rafId) cancelAnimationFrame(rafId);
      ro.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener('webglcontextlost', onLost);
      for (const c of Object.values(cables)) { c.mesh.geometry.dispose(); c.mat.dispose(); }
      for (const o of Object.values(feederObjs)) { if (o.labelTex) o.labelTex.dispose(); o.labelSprite.material.dispose(); }
      if (hotLabelTex) hotLabelTex.dispose();
      hotLabel.material.dispose();
      for (const d of disposables) if (d && d.dispose) d.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
