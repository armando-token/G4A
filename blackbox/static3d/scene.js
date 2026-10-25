import * as THREE from 'three';

/**
 * Deterministic PRNG (Mulberry32) so procedural details are 100% reproducible.
 */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Maps normalized temperature [0..1] to false-color ironbow thermal RGB.
 */
function thermalColor(t) {
  const x = Math.max(0, Math.min(1, t));
  let r = 0, g = 0, b = 0;
  if (x < 0.25) {
    const u = x / 0.25;
    r = 0.08 * u;
    g = 0.02 * u;
    b = 0.15 + 0.55 * u;
  } else if (x < 0.55) {
    const u = (x - 0.25) / 0.30;
    r = 0.08 + 0.72 * u;
    g = 0.02 + 0.10 * u;
    b = 0.70 - 0.25 * u;
  } else if (x < 0.82) {
    const u = (x - 0.55) / 0.27;
    r = 0.80 + 0.18 * u;
    g = 0.12 + 0.62 * u;
    b = 0.45 - 0.40 * u;
  } else {
    const u = (x - 0.82) / 0.18;
    r = 0.98 + 0.02 * u;
    g = 0.74 + 0.26 * u;
    b = 0.05 + 0.90 * u;
  }
  return new THREE.Color(r, g, b);
}

export const CAMERA_POSES = {
  aisle_overview: {
    position: [0.0, 1.65, 3.6],
    target: [0.0, 1.25, -1.8],
    fov: 52
  },
  pdu_n2_closeup: {
    position: [-0.62, 1.45, 0.95],
    target: [-1.18, 1.35, -0.45],
    fov: 40
  },
  pdu_n3_closeup: {
    position: [0.62, 1.45, 0.95],
    target: [1.18, 1.35, -0.45],
    fov: 40
  },
  cable_tray_overhead: {
    position: [0.0, 2.38, 1.95],
    target: [0.0, 2.15, -1.6],
    fov: 48
  }
};

export function parseSceneParams(searchString = window.location.search) {
  const params = new URLSearchParams(searchString);
  const mode = params.get('mode') === 'thermal' ? 'thermal' : 'rgb';
  const poseKey = params.get('pose') || 'aisle_overview';
  const width = parseInt(params.get('width') || '1280', 10);
  const height = parseInt(params.get('height') || '720', 10);
  const n2TempC = parseFloat(params.get('n2TempC') || '48.5');
  const n3TempC = parseFloat(params.get('n3TempC') || '84.2');
  return { mode, poseKey, width, height, n2TempC, n3TempC };
}

function makeMaterial({ mode, rgbHex, roughness = 0.55, metalness = 0.45, emissiveHex = 0x000000, tempNorm = 0.2 }) {
  if (mode === 'thermal') {
    const c = thermalColor(tempNorm);
    return new THREE.MeshBasicMaterial({ color: c });
  }
  return new THREE.MeshStandardMaterial({
    color: rgbHex,
    roughness,
    metalness,
    emissive: emissiveHex,
    emissiveIntensity: emissiveHex ? 0.65 : 0.0
  });
}

export function buildDataCenterScene(config) {
  const { mode, n2TempC, n3TempC } = config;
  const rand = mulberry32(0x424c4b42); // deterministic seed "BLKB"
  const scene = new THREE.Scene();

  scene.background = new THREE.Color(mode === 'thermal' ? 0x05020f : 0x080c12);
  if (mode === 'rgb') {
    scene.fog = new THREE.FogExp2(0x080c12, 0.09);
  }

  // Lighting for RGB mode
  if (mode === 'rgb') {
    const ambient = new THREE.AmbientLight(0x9bb8d3, 0.65);
    scene.add(ambient);

    const keyLight = new THREE.DirectionalLight(0xeef6ff, 1.35);
    keyLight.position.set(1.5, 4.2, 3.0);
    scene.add(keyLight);

    const aisleStrip1 = new THREE.PointLight(0x7fd4ff, 1.4, 8.0);
    aisleStrip1.position.set(0, 2.55, 1.0);
    scene.add(aisleStrip1);

    const aisleStrip2 = new THREE.PointLight(0xff6b4a, 0.9, 6.0);
    aisleStrip2.position.set(1.1, 1.5, -0.5);
    scene.add(aisleStrip2);
  }

  // Raised Floor
  const floorGeo = new THREE.PlaneGeometry(6.0, 10.0, 12, 20);
  const floorMat = makeMaterial({
    mode,
    rgbHex: 0x161d26,
    roughness: 0.7,
    metalness: 0.25,
    tempNorm: 0.14
  });
  const floor = new THREE.Mesh(floorGeo, floorMat);
  floor.rotation.x = -Math.PI / 2;
  scene.add(floor);

  // Floor Grid Lines
  const gridHelper = new THREE.GridHelper(
    10.0,
    20,
    mode === 'thermal' ? 0x261844 : 0x2a394a,
    mode === 'thermal' ? 0x191030 : 0x1d2836
  );
  gridHelper.position.y = 0.002;
  scene.add(gridHelper);

  const normTemp = (degC) => Math.max(0.05, Math.min(0.99, (degC - 22.0) / (95.0 - 22.0)));
  const n2Norm = normTemp(n2TempC);
  const n3Norm = normTemp(n3TempC);

  // Build two opposing rows of 42U Racks along the rear hot aisle (left = Feed N2 side, right = Feed N3 side)
  const rackDepth = 1.05;
  const rackWidth = 0.62;
  const rackHeight = 2.1;
  const rackZPositions = [-2.4, -1.2, 0.0, 1.2];

  rackZPositions.forEach((zPos, idx) => {
    [-1, 1].forEach((side) => {
      const rackGroup = new THREE.Group();
      const xCenter = side * 1.45;
      rackGroup.position.set(xCenter, 0, zPos);

      // Rack outer frame
      const frameGeo = new THREE.BoxGeometry(rackDepth, rackHeight, rackWidth);
      const frameMat = makeMaterial({
        mode,
        rgbHex: 0x1f2733,
        roughness: 0.5,
        metalness: 0.6,
        tempNorm: 0.22 + (side > 0 ? 0.06 : 0.02)
      });
      const frameMesh = new THREE.Mesh(frameGeo, frameMat);
      frameMesh.position.y = rackHeight / 2;
      rackGroup.add(frameMesh);

      // 12 Server Chassis per rack
      for (let u = 0; u < 12; u++) {
        const yPos = 0.18 + u * 0.15;
        const loadFactor = 0.32 + 0.35 * rand() + (side > 0 && idx === 2 ? 0.22 : 0.0);
        const chassisGeo = new THREE.BoxGeometry(0.12, 0.11, rackWidth * 0.86);
        const chassisMat = makeMaterial({
          mode,
          rgbHex: 0x2d3746,
          roughness: 0.4,
          metalness: 0.7,
          tempNorm: Math.min(0.92, loadFactor)
        });
        const chassis = new THREE.Mesh(chassisGeo, chassisMat);
        chassis.position.set(-side * (rackDepth / 2 + 0.02), yPos, 0);
        rackGroup.add(chassis);

        // Status LED / exhaust hotspot
        const ledGeo = new THREE.BoxGeometry(0.02, 0.025, 0.04);
        const isFaultSide = side > 0 && idx === 2 && u >= 6 && u <= 9;
        const ledMat = makeMaterial({
          mode,
          rgbHex: isFaultSide ? 0xff3b2f : 0x26e67c,
          emissiveHex: isFaultSide ? 0xff3b2f : 0x18b25d,
          tempNorm: isFaultSide ? 0.91 : loadFactor + 0.1
        });
        const led = new THREE.Mesh(ledGeo, ledMat);
        led.position.set(-side * (rackDepth / 2 + 0.085), yPos, rackWidth * 0.32);
        rackGroup.add(led);
      }

      // Vertical PDU strip mounted inside rear corner of each rack (N2 on left row, N3 on right row)
      const isPrimaryHotPdu = side > 0 && idx === 2;
      const pduTemp = side < 0 ? n2Norm : (isPrimaryHotPdu ? n3Norm : n3Norm * 0.86);
      const pduGeo = new THREE.BoxGeometry(0.09, 1.92, 0.09);
      const pduMat = makeMaterial({
        mode,
        rgbHex: side < 0 ? 0x1d4e89 : 0x9e2a2b,
        roughness: 0.35,
        metalness: 0.5,
        emissiveHex: isPrimaryHotPdu ? 0x551100 : 0x000000,
        tempNorm: pduTemp
      });
      const pduStrip = new THREE.Mesh(pduGeo, pduMat);
      pduStrip.name = side < 0 ? `PDU_N2_RACK_${idx}` : `PDU_N3_RACK_${idx}`;
      pduStrip.position.set(-side * (rackDepth / 2 + 0.05), 1.02, -rackWidth * 0.38);
      rackGroup.add(pduStrip);

      // Feeder Whip Conduit connecting PDU to overhead busway
      const whipGeo = new THREE.CylinderGeometry(0.028, 0.028, 0.65, 12);
      const whipMat = makeMaterial({
        mode,
        rgbHex: side < 0 ? 0x2b6cb0 : 0xc53030,
        roughness: 0.45,
        metalness: 0.3,
        tempNorm: pduTemp * 0.96
      });
      const whip = new THREE.Mesh(whipGeo, whipMat);
      whip.position.set(-side * (rackDepth / 2 + 0.05), 2.22, -rackWidth * 0.38);
      rackGroup.add(whip);

      scene.add(rackGroup);
    });
  });

  // Overhead Cable Trays & Busways (Feeder N2 left, Feeder N3 right)
  [-1, 1].forEach((side) => {
    const busNorm = side < 0 ? n2Norm : n3Norm;
    const trayGeo = new THREE.BoxGeometry(0.32, 0.08, 6.2);
    const trayMat = makeMaterial({
      mode,
      rgbHex: side < 0 ? 0x254e77 : 0x7c2d25,
      roughness: 0.4,
      metalness: 0.65,
      tempNorm: busNorm * 0.92
    });
    const tray = new THREE.Mesh(trayGeo, trayMat);
    tray.name = side < 0 ? 'BUSWAY_FEEDER_N2' : 'BUSWAY_FEEDER_N3';
    tray.position.set(side * 0.85, 2.48, -0.6);
    scene.add(tray);
  });

  // Cross-aisle ladder rungs
  for (let z = -2.8; z <= 1.8; z += 0.65) {
    const rungGeo = new THREE.BoxGeometry(1.75, 0.035, 0.06);
    const rungMat = makeMaterial({
      mode,
      rgbHex: 0x3a4756,
      roughness: 0.5,
      metalness: 0.7,
      tempNorm: 0.34
    });
    const rung = new THREE.Mesh(rungGeo, rungMat);
    rung.position.set(0, 2.42, z);
    scene.add(rung);
  }

  return scene;
}

export function renderDeterministicScene() {
  const config = parseSceneParams();
  const canvas = document.getElementById('viewport');
  const hud = document.getElementById('hud');

  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    preserveDrawingBuffer: true,
    powerPreference: 'high-performance'
  });
  renderer.setPixelRatio(1);
  renderer.setSize(config.width, config.height, false);

  const pose = CAMERA_POSES[config.poseKey] || CAMERA_POSES.aisle_overview;
  const camera = new THREE.PerspectiveCamera(
    pose.fov,
    config.width / config.height,
    0.1,
    50.0
  );
  camera.position.set(...pose.position);
  camera.lookAt(...pose.target);

  const scene = buildDataCenterScene(config);
  renderer.render(scene, camera);

  if (hud) {
    hud.textContent = `BLACKBOX-DC // MODE=${config.mode.toUpperCase()} // POSE=${config.poseKey.toUpperCase()} // N2=${config.n2TempC.toFixed(1)}C // N3=${config.n3TempC.toFixed(1)}C`;
  }

  window.__BLACKBOX_SCENE_READY__ = true;
  window.__BLACKBOX_RENDER_META__ = {
    mode: config.mode,
    pose: config.poseKey,
    width: config.width,
    height: config.height,
    n2TempC: config.n2TempC,
    n3TempC: config.n3TempC
  };
}

if (typeof window !== 'undefined') {
  renderDeterministicScene();
}
