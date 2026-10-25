import * as THREE from 'three';

export const VIEWPORT_WIDTH = 1280;
export const VIEWPORT_HEIGHT = 800;

export const RACK_SPECS = [
  { id: 'N1', centerX: 0.00, baseTempC: 31.5 },
  { id: 'N2', centerX: 0.82, baseTempC: 28.0 },
  { id: 'N3', centerX: 1.64, baseTempC: 29.0 },
  { id: 'N4', centerX: 2.46, baseTempC: 34.0 }
];

export const FAULT_CONNECTOR_SPEC = {
  rackId: 'N4',
  center: [2.57, 1.20, 0.555],
  size: [0.15, 0.19, 0.06],
  peakTempC: 88.95030198217694,
  sigmaM: 0.062
};

export const REFERENCE_CAMERA_SPEC = {
  position: [1.23, 1.24, 3.45],
  lookAt: [1.23, 1.08, 0.50],
  fov: 43,
  aspect: VIEWPORT_WIDTH / VIEWPORT_HEIGHT,
  near: 0.1,
  far: 25.0
};

/**
 * Deterministic Mulberry32 PRNG.
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
 * Create a deterministic perforated rear-door mesh canvas texture.
 */
function createPerforatedDoorTexture() {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 512;
  const ctx = c.getContext('2d');

  ctx.fillStyle = '#1b222c';
  ctx.fillRect(0, 0, c.width, c.height);

  // Border stiles
  ctx.strokeStyle = '#2c3746';
  ctx.lineWidth = 14;
  ctx.strokeRect(7, 7, c.width - 14, c.height - 14);

  // Hex/grid perforation pattern
  ctx.fillStyle = '#0b0f15';
  for (let y = 22; y < c.height - 22; y += 8) {
    const offset = ((y / 8) % 2) * 4;
    for (let x = 20 + offset; x < c.width - 20; x += 8) {
      ctx.fillRect(x, y, 4, 4);
    }
  }

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Create a high-contrast rack identifier placard texture ("N1".."N4").
 */
function createRackLabelTexture(rackId) {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 96;
  const ctx = c.getContext('2d');

  ctx.fillStyle = '#0f1722';
  ctx.fillRect(0, 0, c.width, c.height);

  ctx.strokeStyle = '#58a6ff';
  ctx.lineWidth = 6;
  ctx.strokeRect(4, 4, c.width - 8, c.height - 8);

  ctx.fillStyle = '#f0f6fc';
  ctx.font = 'bold 58px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(rackId, c.width / 2, c.height / 2 + 2);

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Create the custom ShaderMaterial used across all scene meshes in thermal mode.
 * Computes 3D distance to the N4 fault connector at [2.57, 1.2, 0.555] with
 * Gaussian sigma = 0.062 m and peak temperature 88.95030198217694 C on a
 * monotonic 20C - 100C false-color scale.
 */
function createThermalShaderMaterial({ baseTempC = 28.0, isConnector = false } = {}) {
  return new THREE.ShaderMaterial({
    uniforms: {
      uHotspotCenter: { value: new THREE.Vector3(...FAULT_CONNECTOR_SPEC.center) },
      uPeakTempC: { value: FAULT_CONNECTOR_SPEC.peakTempC },
      uSigmaM: { value: FAULT_CONNECTOR_SPEC.sigmaM },
      uBaseTempC: { value: baseTempC },
      uMinTempC: { value: 20.0 },
      uMaxTempC: { value: 100.0 },
      uIsConnector: { value: isConnector ? 1.0 : 0.0 }
    },
    vertexShader: /* glsl */ `
      varying vec3 vWorldPos;
      varying vec3 vWorldNormal;
      void main() {
        vec4 worldPosition = modelMatrix * vec4(position, 1.0);
        vWorldPos = worldPosition.xyz;
        vWorldNormal = normalize(mat3(modelMatrix) * normal);
        gl_Position = projectionMatrix * viewMatrix * worldPosition;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uHotspotCenter;
      uniform float uPeakTempC;
      uniform float uSigmaM;
      uniform float uBaseTempC;
      uniform float uMinTempC;
      uniform float uMaxTempC;
      uniform float uIsConnector;

      varying vec3 vWorldPos;
      varying vec3 vWorldNormal;

      vec3 palette20to100(float u, float coreBoost) {
        float x = clamp(u, 0.0, 1.0);
        vec3 col;
        if (x < 0.25) {
          float k = x / 0.25;
          col = mix(vec3(0.06, 0.04, 0.20), vec3(0.11, 0.20, 0.53), k);
        } else if (x < 0.50) {
          float k = (x - 0.25) / 0.25;
          col = mix(vec3(0.11, 0.20, 0.53), vec3(0.50, 0.12, 0.45), k);
        } else if (x < 0.72) {
          float k = (x - 0.50) / 0.22;
          col = mix(vec3(0.50, 0.12, 0.45), vec3(0.82, 0.36, 0.12), k);
        } else if (x < 0.855) {
          float k = (x - 0.72) / 0.135;
          col = mix(vec3(0.82, 0.36, 0.12), vec3(0.95, 0.84, 0.38), k);
        } else {
          float k = clamp((x - 0.855) / (0.86188 - 0.855), 0.0, 1.0);
          col = mix(vec3(0.95, 0.84, 0.38), vec3(0.992, 0.985, 0.96), k);
        }
        col = mix(col, vec3(1.0, 1.0, 1.0), coreBoost);
        return clamp(col, 0.0, 1.0);
      }

      void main() {
        // For the connector housing box, evaluate the 3D Gaussian on the connector center plane z=0.555
        // along the camera ray so the 3D center [2.57, 1.2, 0.555] projects without front-face parallax.
        vec3 evalPos = vWorldPos;
        if (uIsConnector > 0.5) {
          float dz = vWorldPos.z - cameraPosition.z;
          if (abs(dz) > 1e-4) {
            float tPlane = (uHotspotCenter.z - cameraPosition.z) / dz;
            evalPos = cameraPosition + (vWorldPos - cameraPosition) * tPlane;
          }
        }

        float d = length(evalPos - uHotspotCenter);
        float gauss = exp(-(d * d) / (2.0 * uSigmaM * uSigmaM));

        // Subtle geometric shading on cold background structures so rack silhouettes remain legible
        float rim = 0.8 * abs(vWorldNormal.z) + 0.2 * abs(vWorldNormal.y);
        float localBaseC = uBaseTempC + 1.2 * rim * (1.0 - gauss);

        float tempC = localBaseC + (uPeakTempC - localBaseC) * gauss;
        float u = (tempC - uMinTempC) / (uMaxTempC - uMinTempC);

        // Pinpoint sub-millimeter core peak at d=0 on the N4 connector
        float coreBoost = uIsConnector * exp(-(d * d) / (2.0 * 0.0022 * 0.0022));
        vec3 rgb = palette20to100(u, coreBoost);
        gl_FragColor = vec4(rgb, 1.0);
      }
    `
  });
}

/**
 * Project a 3D world point [x, y, z] into 2D viewport pixel coordinates [px, py].
 */
export function projectWorldToPixel(worldPt, camera, width = VIEWPORT_WIDTH, height = VIEWPORT_HEIGHT) {
  const v = new THREE.Vector3(worldPt[0], worldPt[1], worldPt[2]);
  v.project(camera);
  const px = (v.x * 0.5 + 0.5) * width;
  const py = (-v.y * 0.5 + 0.5) * height;
  return [Number(px.toFixed(2)), Number(py.toFixed(2))];
}

/**
 * Compute projected 2D pixel bounding box [xMin, yMin, xMax, yMax] of a 3D box.
 */
export function computeProjectedBoxBBox(center, size, camera, width = VIEWPORT_WIDTH, height = VIEWPORT_HEIGHT) {
  const [cx, cy, cz] = center;
  const [sx, sy, sz] = size;
  const hx = sx / 2;
  const hy = sy / 2;
  const hz = sz / 2;

  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  for (const dx of [-hx, hx]) {
    for (const dy of [-hy, hy]) {
      for (const dz of [-hz, hz]) {
        const [px, py] = projectWorldToPixel([cx + dx, cy + dy, cz + dz], camera, width, height);
        if (px < minX) minX = px;
        if (py < minY) minY = py;
        if (px > maxX) maxX = px;
        if (py > maxY) maxY = py;
      }
    }
  }
  return [
    Math.round(minX),
    Math.round(minY),
    Math.round(maxX),
    Math.round(maxY)
  ];
}

/**
 * Build the complete deterministic rear-aisle 3D scene with dual RGB and Thermal materials.
 */
function createSceneGraph() {
  const rand = mulberry32(0x424c4b32);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x090d14);

  const meshRegistry = [];
  const registerMesh = (mesh, rgbMaterial, thermalMaterial) => {
    mesh.userData.rgbMaterial = rgbMaterial;
    mesh.userData.thermalMaterial = thermalMaterial;
    mesh.material = rgbMaterial;
    meshRegistry.push(mesh);
    return mesh;
  };

  // Lights for RGB mode
  const lightGroup = new THREE.Group();
  const ambientLight = new THREE.AmbientLight(0xd4e4f7, 1.15);
  lightGroup.add(ambientLight);

  const keyLight = new THREE.DirectionalLight(0xf5f9ff, 1.85);
  keyLight.position.set(1.23, 3.6, 4.2);
  lightGroup.add(keyLight);

  const fillLightLeft = new THREE.PointLight(0x8ec5ff, 1.35, 9.0);
  fillLightLeft.position.set(-0.4, 2.2, 2.5);
  lightGroup.add(fillLightLeft);

  const fillLightRight = new THREE.PointLight(0x9ad0ff, 1.45, 9.0);
  fillLightRight.position.set(2.8, 2.2, 2.5);
  lightGroup.add(fillLightRight);

  scene.add(lightGroup);

  // Raised Floor (y = 0)
  const floorGeo = new THREE.PlaneGeometry(8.0, 7.0);
  const floorRgbMat = new THREE.MeshStandardMaterial({
    color: 0x161d27,
    roughness: 0.72,
    metalness: 0.22
  });
  const floorThermMat = createThermalShaderMaterial({ baseTempC: 23.5 });
  const floorMesh = registerMesh(new THREE.Mesh(floorGeo, floorRgbMat), floorRgbMat, floorThermMat);
  floorMesh.rotation.x = -Math.PI / 2;
  floorMesh.position.set(1.23, 0.0, 1.2);
  scene.add(floorMesh);

  // Subtle floor tile grid
  const gridHelper = new THREE.GridHelper(7.0, 14, 0x283546, 0x1e2938);
  gridHelper.position.set(1.23, 0.002, 1.2);
  scene.add(gridHelper);

  const perfTexture = createPerforatedDoorTexture();

  // Four Cabinets N1..N4
  for (const spec of RACK_SPECS) {
    const rackGroup = new THREE.Group();
    rackGroup.name = `RACK_${spec.id}`;

    // Cabinet body: 0.6m x 2.0m x 1.0m centered at (centerX, 1.0, 0.0) so rear face is at z = +0.5
    const bodyGeo = new THREE.BoxGeometry(0.60, 2.00, 1.00);
    const bodyRgbMat = new THREE.MeshStandardMaterial({
      color: 0x212a36,
      roughness: 0.52,
      metalness: 0.58
    });
    const bodyThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC });
    const bodyMesh = registerMesh(new THREE.Mesh(bodyGeo, bodyRgbMat), bodyRgbMat, bodyThermMat);
    bodyMesh.position.set(spec.centerX, 1.00, 0.00);
    rackGroup.add(bodyMesh);

    // Perforated rear door insert panel on z = +0.502
    const doorGeo = new THREE.PlaneGeometry(0.52, 1.66);
    const doorRgbMat = new THREE.MeshStandardMaterial({
      map: perfTexture,
      roughness: 0.6,
      metalness: 0.45
    });
    const doorThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 1.0 });
    const doorMesh = registerMesh(new THREE.Mesh(doorGeo, doorRgbMat), doorRgbMat, doorThermMat);
    doorMesh.position.set(spec.centerX, 0.92, 0.502);
    rackGroup.add(doorMesh);

    // Server chassis faceplates visible behind rear frame rails
    for (let slot = 0; slot < 8; slot++) {
      const slotY = 0.24 + slot * 0.19;
      const srvGeo = new THREE.BoxGeometry(0.46, 0.12, 0.02);
      const srvRgbMat = new THREE.MeshStandardMaterial({
        color: 0x2d3848,
        roughness: 0.42,
        metalness: 0.65
      });
      const srvThermMat = createThermalShaderMaterial({
        baseTempC: spec.baseTempC + 1.5 + 0.8 * rand()
      });
      const srvMesh = registerMesh(new THREE.Mesh(srvGeo, srvRgbMat), srvRgbMat, srvThermMat);
      srvMesh.position.set(spec.centerX - 0.02, slotY, 0.506);
      rackGroup.add(srvMesh);
    }

    // Vertical rear PDU busbar channel along right side of each cabinet (x = centerX + 0.11, z = 0.515)
    const pduGeo = new THREE.BoxGeometry(0.065, 1.78, 0.03);
    const pduRgbMat = new THREE.MeshStandardMaterial({
      color: 0x39475b,
      roughness: 0.38,
      metalness: 0.72
    });
    const pduThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 1.8 });
    const pduMesh = registerMesh(new THREE.Mesh(pduGeo, pduRgbMat), pduRgbMat, pduThermMat);
    pduMesh.position.set(spec.centerX + 0.11, 0.96, 0.515);
    rackGroup.add(pduMesh);

    // Overhead feeder whip conduit rising from cabinet top to cable tray
    const whipGeo = new THREE.CylinderGeometry(0.022, 0.022, 0.32, 12);
    const whipRgbMat = new THREE.MeshStandardMaterial({
      color: 0x4b5d75,
      roughness: 0.45,
      metalness: 0.55
    });
    const whipThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 0.5 });
    const whipMesh = registerMesh(new THREE.Mesh(whipGeo, whipRgbMat), whipRgbMat, whipThermMat);
    whipMesh.position.set(spec.centerX + 0.11, 2.15, 0.42);
    rackGroup.add(whipMesh);

    // High-contrast rack header label placard ("N1".."N4") at top of rear face
    const labelGeo = new THREE.PlaneGeometry(0.32, 0.12);
    const labelTex = createRackLabelTexture(spec.id);
    const labelRgbMat = new THREE.MeshBasicMaterial({ map: labelTex });
    const labelThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 2.2 });
    const labelMesh = registerMesh(new THREE.Mesh(labelGeo, labelRgbMat), labelRgbMat, labelThermMat);
    labelMesh.position.set(spec.centerX, 1.89, 0.508);
    rackGroup.add(labelMesh);

    // Standard healthy connector housing on N1, N2, N3 at matching height y = 1.20
    if (spec.id !== 'N4') {
      const stdConnGeo = new THREE.BoxGeometry(0.12, 0.15, 0.045);
      const stdConnRgbMat = new THREE.MeshStandardMaterial({
        color: 0x435368,
        roughness: 0.4,
        metalness: 0.68
      });
      const stdConnThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 2.0 });
      const stdConn = registerMesh(new THREE.Mesh(stdConnGeo, stdConnRgbMat), stdConnRgbMat, stdConnThermMat);
      stdConn.position.set(spec.centerX + 0.11, 1.20, 0.545);
      rackGroup.add(stdConn);
    }

    scene.add(rackGroup);
  }

  // Overhead horizontal cable tray spanning x = -0.45 to 2.95 at y = 2.28, z = 0.42
  const trayGeo = new THREE.BoxGeometry(3.45, 0.07, 0.28);
  const trayRgbMat = new THREE.MeshStandardMaterial({
    color: 0x2f3b4c,
    roughness: 0.48,
    metalness: 0.62
  });
  const trayThermMat = createThermalShaderMaterial({ baseTempC: 26.5 });
  const trayMesh = registerMesh(new THREE.Mesh(trayGeo, trayRgbMat), trayRgbMat, trayThermMat);
  trayMesh.position.set(1.23, 2.28, 0.42);
  scene.add(trayMesh);

  // Fault connector on N4: exact center [2.57, 1.2, 0.555], size 0.15 x 0.19 x 0.06 m
  const faultGeo = new THREE.BoxGeometry(...FAULT_CONNECTOR_SPEC.size);
  const faultRgbMat = new THREE.MeshStandardMaterial({
    color: 0x56667a,
    roughness: 0.36,
    metalness: 0.74
  });
  const faultThermMat = createThermalShaderMaterial({
    baseTempC: 34.0,
    isConnector: true
  });
  const faultMesh = registerMesh(new THREE.Mesh(faultGeo, faultRgbMat), faultRgbMat, faultThermMat);
  faultMesh.name = 'N4_FAULT_CONNECTOR';
  faultMesh.position.set(...FAULT_CONNECTOR_SPEC.center);
  scene.add(faultMesh);

  return { scene, meshRegistry, lightGroup, gridHelper };
}

/**
 * Initialize renderer, reference camera, mode switcher, and metadata exports.
 */
export function initBlackboxScene() {
  const params = new URLSearchParams(window.location.search);
  const initialMode = params.get('mode') === 'thermal' ? 'thermal' : 'rgb';

  const canvas = document.getElementById('viewport');
  const legendEl = document.getElementById('thermal-legend');

  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: false,
    preserveDrawingBuffer: true,
    powerPreference: 'high-performance'
  });
  renderer.setPixelRatio(1);
  renderer.setSize(VIEWPORT_WIDTH, VIEWPORT_HEIGHT, false);
  renderer.toneMapping = THREE.NoToneMapping;
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const camera = new THREE.PerspectiveCamera(
    REFERENCE_CAMERA_SPEC.fov,
    REFERENCE_CAMERA_SPEC.aspect,
    REFERENCE_CAMERA_SPEC.near,
    REFERENCE_CAMERA_SPEC.far
  );
  camera.position.set(...REFERENCE_CAMERA_SPEC.position);
  camera.lookAt(...REFERENCE_CAMERA_SPEC.lookAt);
  camera.updateMatrixWorld(true);
  camera.updateProjectionMatrix();

  const { scene, meshRegistry, lightGroup, gridHelper } = createSceneGraph();

  function applyModeAndRender(mode) {
    const normalizedMode = mode === 'thermal' ? 'thermal' : 'rgb';
    scene.background = new THREE.Color(normalizedMode === 'thermal' ? 0x060412 : 0x090d14);
    lightGroup.visible = normalizedMode === 'rgb';
    gridHelper.visible = normalizedMode === 'rgb';

    for (const mesh of meshRegistry) {
      mesh.material = normalizedMode === 'thermal'
        ? mesh.userData.thermalMaterial
        : mesh.userData.rgbMaterial;
    }

    if (legendEl) {
      legendEl.style.display = normalizedMode === 'thermal' ? 'block' : 'none';
    }

    camera.updateMatrixWorld(true);
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);

    const projectedCenter = projectWorldToPixel(FAULT_CONNECTOR_SPEC.center, camera);
    const projectedBBox = computeProjectedBoxBBox(
      FAULT_CONNECTOR_SPEC.center,
      FAULT_CONNECTOR_SPEC.size,
      camera
    );

    const meta = {
      mode: normalizedMode,
      threeVersion: THREE.REVISION,
      viewport: {
        width: VIEWPORT_WIDTH,
        height: VIEWPORT_HEIGHT,
        devicePixelRatio: 1
      },
      camera: {
        position: [...camera.position.toArray()],
        lookAt: [...REFERENCE_CAMERA_SPEC.lookAt],
        fov: camera.fov,
        aspect: camera.aspect,
        near: camera.near,
        far: camera.far,
        matrixWorld: camera.matrixWorld.toArray().map((v) => Number(v.toFixed(6))),
        projectionMatrix: camera.projectionMatrix.toArray().map((v) => Number(v.toFixed(6)))
      },
      racks: RACK_SPECS.map((r) => ({
        id: r.id,
        center: [r.centerX, 1.0, 0.0],
        dimensions: [0.6, 2.0, 1.0]
      })),
      faultConnector: {
        rackId: FAULT_CONNECTOR_SPEC.rackId,
        worldCenter: [...FAULT_CONNECTOR_SPEC.center],
        size: [...FAULT_CONNECTOR_SPEC.size],
        projectedPixelCenter: projectedCenter,
        projectedPixelBBox: projectedBBox,
        peakTempC: FAULT_CONNECTOR_SPEC.peakTempC,
        sigmaM: FAULT_CONNECTOR_SPEC.sigmaM
      },
      lighting: {
        toneMapping: 'NoToneMapping',
        outputColorSpace: 'srgb',
        ambientIntensity: 1.15,
        keyLightIntensity: 1.85,
        keyLightPosition: [1.23, 3.6, 4.2]
      },
      thermalScale: {
        label: 'SYNTHETIC TEMPERATURE: 20°C — 100°C',
        minTempC: 20.0,
        maxTempC: 100.0,
        peakTempC: FAULT_CONNECTOR_SPEC.peakTempC,
        sigmaM: FAULT_CONNECTOR_SPEC.sigmaM
      }
    };

    window.__BLACKBOX_RENDER_META__ = Object.freeze(meta);
    return meta;
  }

  window.setRenderMode = (mode) => applyModeAndRender(mode);

  window.setCameraPose = (position, lookAt = REFERENCE_CAMERA_SPEC.lookAt) => {
    camera.position.set(position[0], position[1], position[2]);
    camera.lookAt(lookAt[0], lookAt[1], lookAt[2]);
    const currentMode = window.__BLACKBOX_RENDER_META__?.mode || initialMode;
    return applyModeAndRender(currentMode);
  };

  applyModeAndRender(initialMode);
  window.__BLACKBOX_SCENE_READY__ = true;
}

if (typeof window !== 'undefined') {
  initBlackboxScene();
}
