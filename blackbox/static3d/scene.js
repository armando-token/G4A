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

export const CAMERA_POSES = {
  rear_inspection: {
    position: [1.23, 1.24, 3.45],
    lookAt: [1.23, 1.08, 0.50],
    fov: 43
  },
  wiring_oblique: {
    position: [1.23, 1.68, 3.55],
    lookAt: [1.23, 1.55, 0.40],
    fov: 43
  }
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
 * Create a high-contrast feeder busbar or cable badge texture ("F1 (44A)", "F2 (50A)", "F1", "F2").
 */
function createFeederPlacardTexture(labelText, borderColorHex = '#1d8aff') {
  const c = document.createElement('canvas');
  c.width = 256;
  c.height = 88;
  const ctx = c.getContext('2d');

  ctx.fillStyle = '#0b111a';
  ctx.fillRect(0, 0, c.width, c.height);

  ctx.strokeStyle = borderColorHex;
  ctx.lineWidth = 7;
  ctx.strokeRect(4, 4, c.width - 8, c.height - 8);

  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 42px monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(String(labelText), c.width / 2, c.height / 2 + 2);

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/**
 * Create the custom ShaderMaterial used across all scene meshes in thermal mode.
 */
function createThermalShaderMaterial({
  baseTempC = 28.0,
  isConnector = false,
  hotspotCenter = FAULT_CONNECTOR_SPEC.center,
  peakTempC = FAULT_CONNECTOR_SPEC.peakTempC,
  sigmaM = FAULT_CONNECTOR_SPEC.sigmaM
} = {}) {
  return new THREE.ShaderMaterial({
    uniforms: {
      uHotspotCenter: { value: new THREE.Vector3(...hotspotCenter) },
      uPeakTempC: { value: peakTempC },
      uSigmaM: { value: sigmaM },
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

        float rim = 0.8 * abs(vWorldNormal.z) + 0.2 * abs(vWorldNormal.y);
        float localBaseC = uBaseTempC + 1.2 * rim * (1.0 - gauss);

        float tempC = localBaseC + (uPeakTempC - localBaseC) * gauss;
        float u = (tempC - uMinTempC) / (uMaxTempC - uMinTempC);

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
 * Legacy no-spec scene builder (preserves exact Phase 2 SHA-256 digests).
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

    const labelGeo = new THREE.PlaneGeometry(0.32, 0.12);
    const labelTex = createRackLabelTexture(spec.id);
    const labelRgbMat = new THREE.MeshBasicMaterial({ map: labelTex });
    const labelThermMat = createThermalShaderMaterial({ baseTempC: spec.baseTempC + 2.2 });
    const labelMesh = registerMesh(new THREE.Mesh(labelGeo, labelRgbMat), labelRgbMat, labelThermMat);
    labelMesh.position.set(spec.centerX, 1.89, 0.508);
    rackGroup.add(labelMesh);

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

  return { scene, meshRegistry, lightGroup, gridHelper, cableWorldCorridors: null, faultSpec: FAULT_CONNECTOR_SPEC };
}

/**
 * Build the parameterized SceneSpec v1 3D scene with explicit F1/F2 overhead busbars
 * and graph-coupled 3D branch feeder cables.
 */
function createParameterizedSceneGraph(specObj) {
  const rand = mulberry32(0x424c4b32);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x090d14);

  const hotspotSpec = specObj?.hotspot || {};
  const hotRackId = String(hotspotSpec.rack_id || 'N4');
  const hotPeakTempC = Number(hotspotSpec.temperature_c ?? FAULT_CONNECTOR_SPEC.peakTempC);
  const hotSigmaM = Number(hotspotSpec.sigma_m ?? FAULT_CONNECTOR_SPEC.sigmaM);

  const rackMap = new Map();
  for (const defRack of RACK_SPECS) {
    rackMap.set(defRack.id, {
      id: defRack.id,
      centerX: defRack.centerX,
      widthM: 0.60,
      heightM: 2.00,
      depthM: 1.00,
      baseTempC: defRack.baseTempC
    });
  }
  if (Array.isArray(specObj?.racks)) {
    for (const r of specObj.racks) {
      if (r && rackMap.has(String(r.rack_id))) {
        const cur = rackMap.get(String(r.rack_id));
        cur.centerX = Number(r.center_x_m ?? cur.centerX);
        cur.widthM = Number(r.width_m ?? cur.widthM);
        cur.heightM = Number(r.height_m ?? cur.heightM);
        cur.depthM = Number(r.depth_m ?? cur.depthM);
        cur.baseTempC = Number(r.body_temp_c ?? cur.baseTempC);
      }
    }
  }

  const hotRackCenter = rackMap.get(hotRackId)?.centerX ?? 2.46;
  const faultSpec = {
    rackId: hotRackId,
    center: [Number((hotRackCenter + 0.11).toFixed(4)), 1.20, 0.555],
    size: [...FAULT_CONNECTOR_SPEC.size],
    peakTempC: hotPeakTempC,
    sigmaM: hotSigmaM
  };

  const makeThermalMat = ({ baseTempC = 28.0, isConnector = false } = {}) =>
    createThermalShaderMaterial({
      baseTempC,
      isConnector,
      hotspotCenter: faultSpec.center,
      peakTempC: faultSpec.peakTempC,
      sigmaM: faultSpec.sigmaM
    });

  const meshRegistry = [];
  const registerMesh = (mesh, rgbMaterial, thermalMaterial) => {
    mesh.userData.rgbMaterial = rgbMaterial;
    mesh.userData.thermalMaterial = thermalMaterial;
    mesh.material = rgbMaterial;
    meshRegistry.push(mesh);
    return mesh;
  };

  // Lights
  const lightGroup = new THREE.Group();
  lightGroup.add(new THREE.AmbientLight(0xd4e4f7, 1.15));
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

  // Raised Floor
  const floorGeo = new THREE.PlaneGeometry(8.0, 7.0);
  const floorRgbMat = new THREE.MeshStandardMaterial({ color: 0x161d27, roughness: 0.72, metalness: 0.22 });
  const floorMesh = registerMesh(new THREE.Mesh(floorGeo, floorRgbMat), floorRgbMat, makeThermalMat({ baseTempC: 23.5 }));
  floorMesh.rotation.x = -Math.PI / 2;
  floorMesh.position.set(1.23, 0.0, 1.2);
  scene.add(floorMesh);

  const gridHelper = new THREE.GridHelper(7.0, 14, 0x283546, 0x1e2938);
  gridHelper.position.set(1.23, 0.002, 1.2);
  scene.add(gridHelper);

  const perfTexture = createPerforatedDoorTexture();

  // Four Racks N1..N4
  for (const spec of rackMap.values()) {
    const rackGroup = new THREE.Group();
    rackGroup.name = `RACK_${spec.id}`;

    const bodyGeo = new THREE.BoxGeometry(spec.widthM, spec.heightM, spec.depthM);
    const bodyRgbMat = new THREE.MeshStandardMaterial({ color: 0x212a36, roughness: 0.52, metalness: 0.58 });
    const bodyMesh = registerMesh(
      new THREE.Mesh(bodyGeo, bodyRgbMat),
      bodyRgbMat,
      makeThermalMat({ baseTempC: spec.baseTempC })
    );
    bodyMesh.position.set(spec.centerX, 1.00, 0.00);
    rackGroup.add(bodyMesh);

    const doorGeo = new THREE.PlaneGeometry(0.52, 1.66);
    const doorRgbMat = new THREE.MeshStandardMaterial({ map: perfTexture, roughness: 0.6, metalness: 0.45 });
    const doorMesh = registerMesh(
      new THREE.Mesh(doorGeo, doorRgbMat),
      doorRgbMat,
      makeThermalMat({ baseTempC: spec.baseTempC + 1.0 })
    );
    doorMesh.position.set(spec.centerX, 0.92, 0.502);
    rackGroup.add(doorMesh);

    for (let slot = 0; slot < 8; slot++) {
      const slotY = 0.24 + slot * 0.19;
      const srvGeo = new THREE.BoxGeometry(0.46, 0.12, 0.02);
      const srvRgbMat = new THREE.MeshStandardMaterial({ color: 0x2d3848, roughness: 0.42, metalness: 0.65 });
      const srvMesh = registerMesh(
        new THREE.Mesh(srvGeo, srvRgbMat),
        srvRgbMat,
        makeThermalMat({ baseTempC: spec.baseTempC + 1.5 + 0.8 * rand() })
      );
      srvMesh.position.set(spec.centerX - 0.02, slotY, 0.506);
      rackGroup.add(srvMesh);
    }

    const pduGeo = new THREE.BoxGeometry(0.065, 1.78, 0.03);
    const pduRgbMat = new THREE.MeshStandardMaterial({ color: 0x39475b, roughness: 0.38, metalness: 0.72 });
    const pduMesh = registerMesh(
      new THREE.Mesh(pduGeo, pduRgbMat),
      pduRgbMat,
      makeThermalMat({ baseTempC: spec.baseTempC + 1.8 })
    );
    pduMesh.position.set(spec.centerX + 0.11, 0.96, 0.515);
    rackGroup.add(pduMesh);

    const labelGeo = new THREE.PlaneGeometry(0.32, 0.12);
    const labelTex = createRackLabelTexture(spec.id);
    const labelRgbMat = new THREE.MeshBasicMaterial({ map: labelTex });
    const labelMesh = registerMesh(
      new THREE.Mesh(labelGeo, labelRgbMat),
      labelRgbMat,
      makeThermalMat({ baseTempC: spec.baseTempC + 2.2 })
    );
    labelMesh.position.set(spec.centerX, 1.89, 0.508);
    rackGroup.add(labelMesh);

    if (spec.id !== hotRackId) {
      const stdConnGeo = new THREE.BoxGeometry(0.12, 0.15, 0.045);
      const stdConnRgbMat = new THREE.MeshStandardMaterial({ color: 0x435368, roughness: 0.4, metalness: 0.68 });
      const stdConn = registerMesh(
        new THREE.Mesh(stdConnGeo, stdConnRgbMat),
        stdConnRgbMat,
        makeThermalMat({ baseTempC: spec.baseTempC + 2.0 })
      );
      stdConn.position.set(spec.centerX + 0.11, 1.20, 0.545);
      rackGroup.add(stdConn);
    }

    scene.add(rackGroup);
  }

  // Two visibly separate overhead feeder busbars (3D separation = 0.382m >= 0.20m):
  // F1 busbar at y = 2.38m, z = 0.26m (electric blue #1d8aff)
  // F2 busbar at y = 2.12m, z = 0.54m (high-contrast orange #ff8c1a)
  const busGeo = new THREE.BoxGeometry(3.25, 0.055, 0.055);
  const f1BusRgbMat = new THREE.MeshStandardMaterial({
    color: 0x1d8aff,
    emissive: 0x0b3a75,
    roughness: 0.32,
    metalness: 0.45
  });
  const f1BusMesh = registerMesh(
    new THREE.Mesh(busGeo, f1BusRgbMat),
    f1BusRgbMat,
    makeThermalMat({ baseTempC: 26.5 })
  );
  f1BusMesh.name = 'BUSBAR_F1';
  f1BusMesh.position.set(1.23, 2.38, 0.26);
  scene.add(f1BusMesh);

  const f1PlacardGeo = new THREE.PlaneGeometry(0.42, 0.14);
  const f1PlacardMat = new THREE.MeshBasicMaterial({
    map: createFeederPlacardTexture('F1 (44A)', '#1d8aff')
  });
  const f1PlacardMesh = registerMesh(
    new THREE.Mesh(f1PlacardGeo, f1PlacardMat),
    f1PlacardMat,
    makeThermalMat({ baseTempC: 26.8 })
  );
  f1PlacardMesh.position.set(-0.22, 2.38, 0.32);
  scene.add(f1PlacardMesh);

  const f2BusRgbMat = new THREE.MeshStandardMaterial({
    color: 0xff8c1a,
    emissive: 0x7a3800,
    roughness: 0.32,
    metalness: 0.45
  });
  const f2BusMesh = registerMesh(
    new THREE.Mesh(busGeo, f2BusRgbMat),
    f2BusRgbMat,
    makeThermalMat({ baseTempC: 27.0 })
  );
  f2BusMesh.name = 'BUSBAR_F2';
  f2BusMesh.position.set(1.23, 2.12, 0.54);
  scene.add(f2BusMesh);

  const f2PlacardGeo = new THREE.PlaneGeometry(0.42, 0.14);
  const f2PlacardMat = new THREE.MeshBasicMaterial({
    map: createFeederPlacardTexture('F2 (50A)', '#ff8c1a')
  });
  const f2PlacardMesh = registerMesh(
    new THREE.Mesh(f2PlacardGeo, f2PlacardMat),
    f2PlacardMat,
    makeThermalMat({ baseTempC: 27.2 })
  );
  f2PlacardMesh.position.set(2.68, 2.12, 0.58);
  scene.add(f2PlacardMesh);

  // Graph-coupled 3D branch feeder cables derived strictly from specObj.edges
  const edgeMap = { N1: 'F1', N2: 'F1', N3: 'F2', N4: 'F2' };
  if (Array.isArray(specObj?.edges)) {
    for (const e of specObj.edges) {
      if (e && e.rack_id && e.feeder_id) {
        edgeMap[String(e.rack_id)] = String(e.feeder_id);
      }
    }
  }

  const cableWorldCorridors = {};
  for (const spec of rackMap.values()) {
    const rk = spec.id;
    const feederId = edgeMap[rk] === 'F2' ? 'F2' : 'F1';
    const cx = spec.centerX;
    const waypoints =
      feederId === 'F1'
        ? [
            [cx + 0.11, 1.38, 0.53],
            [cx - 0.04, 1.68, 0.44],
            [cx - 0.12, 2.04, 0.34],
            [cx - 0.14, 2.38, 0.26]
          ]
        : [
            [cx + 0.11, 1.38, 0.53],
            [cx + 0.18, 1.60, 0.55],
            [cx + 0.23, 1.88, 0.55],
            [cx + 0.24, 2.12, 0.54]
          ];

    const curvePoints = waypoints.map((pt) => new THREE.Vector3(pt[0], pt[1], pt[2]));
    const curve = new THREE.CatmullRomCurve3(curvePoints);
    const tubeGeo = new THREE.TubeGeometry(curve, 24, 0.030, 12, false);
    const cableRgbMat = new THREE.MeshStandardMaterial({
      color: feederId === 'F1' ? 0x1d8aff : 0xff8c1a,
      emissive: feederId === 'F1' ? 0x0b3a75 : 0x7a3800,
      roughness: 0.35,
      metalness: 0.30
    });
    const cableThermMat = makeThermalMat({ baseTempC: spec.baseTempC + 0.8 });
    const cableMesh = registerMesh(new THREE.Mesh(tubeGeo, cableRgbMat), cableRgbMat, cableThermMat);
    cableMesh.name = `CABLE_${rk}_${feederId}`;
    scene.add(cableMesh);

    // Tap collar on busbar and inlet collar on rack
    const collarGeo = new THREE.SphereGeometry(0.044, 12, 12);
    const tapCollar = registerMesh(new THREE.Mesh(collarGeo, cableRgbMat), cableRgbMat, cableThermMat);
    tapCollar.position.set(...waypoints[waypoints.length - 1]);
    scene.add(tapCollar);

    const inletCollar = registerMesh(new THREE.Mesh(collarGeo, cableRgbMat), cableRgbMat, cableThermMat);
    inletCollar.position.set(...waypoints[0]);
    scene.add(inletCollar);

    const samplePoints = curve.getPoints(16).map((v) => [v.x, v.y, v.z]);
    cableWorldCorridors[rk] = {
      rack_id: rk,
      feeder_id: feederId,
      waypoints_m: waypoints,
      sample_points_m: samplePoints
    };
  }

  // Fault connector on hotRackId
  const faultGeo = new THREE.BoxGeometry(...faultSpec.size);
  const faultRgbMat = new THREE.MeshStandardMaterial({
    color: 0x56667a,
    roughness: 0.36,
    metalness: 0.74
  });
  const faultThermMat = makeThermalMat({
    baseTempC: 34.0,
    isConnector: true
  });
  const faultMesh = registerMesh(new THREE.Mesh(faultGeo, faultRgbMat), faultRgbMat, faultThermMat);
  faultMesh.name = `${hotRackId}_FAULT_CONNECTOR`;
  faultMesh.position.set(...faultSpec.center);
  scene.add(faultMesh);

  return { scene, meshRegistry, lightGroup, gridHelper, cableWorldCorridors, faultSpec, edgeMap };
}

function computeProjectedCableCorridors(cableWorldCorridors, camera) {
  if (!cableWorldCorridors) return null;
  const out = {};
  for (const [rk, info] of Object.entries(cableWorldCorridors)) {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    const waypointsPx = [];
    for (const pt of info.waypoints_m) {
      waypointsPx.push(projectWorldToPixel(pt, camera));
    }
    for (const pt of info.sample_points_m) {
      const [px, py] = projectWorldToPixel(pt, camera);
      if (px < minX) minX = px;
      if (py < minY) minY = py;
      if (px > maxX) maxX = px;
      if (py > maxY) maxY = py;
    }
    const pad = 18;
    out[rk] = {
      rack_id: rk,
      feeder_id: info.feeder_id,
      waypoints_px: waypointsPx,
      bbox_xyxy_px: [
        Math.max(0, Math.round(minX - pad)),
        Math.max(0, Math.round(minY - pad)),
        Math.min(VIEWPORT_WIDTH - 1, Math.round(maxX + pad)),
        Math.min(VIEWPORT_HEIGHT - 1, Math.round(maxY + pad))
      ]
    };
  }
  return out;
}

/**
 * Initialize renderer, reference camera, mode switcher, and metadata exports.
 */
export function initBlackboxScene() {
  const params = new URLSearchParams(window.location.search);
  const initialMode = params.get('mode') === 'thermal' ? 'thermal' : 'rgb';

  const canvas = document.getElementById('viewport');
  const legendEl = document.getElementById('thermal-legend');
  const hudEl = document.getElementById('revision-hud');

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

  let activeState = createSceneGraph();
  let activeSpecObj = null;
  let activeLookAt = [...REFERENCE_CAMERA_SPEC.lookAt];
  let activePoseName = 'rear_inspection';

  function applyModeAndRender(mode) {
    const normalizedMode = mode === 'thermal' ? 'thermal' : 'rgb';
    const { scene, meshRegistry, lightGroup, gridHelper, cableWorldCorridors, faultSpec } = activeState;

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

    if (hudEl) {
      if (activeSpecObj && normalizedMode === 'rgb') {
        const hudText =
          activeSpecObj.hud_label ||
          (activeSpecObj.candidate_target_revision === 'B' || activeSpecObj.source_revision === 'B'
            ? 'Candidate repair toward B'
            : `Twin from ${activeSpecObj.source_revision || 'A'}`);
        hudEl.textContent = String(hudText);
        hudEl.style.display = 'block';
      } else {
        hudEl.style.display = 'none';
      }
    }

    camera.updateMatrixWorld(true);
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);

    const projectedCenter = projectWorldToPixel(faultSpec.center, camera);
    const projectedBBox = computeProjectedBoxBBox(
      faultSpec.center,
      faultSpec.size,
      camera
    );
    const projectedCorridors = computeProjectedCableCorridors(cableWorldCorridors, camera);

    const meta = {
      mode: normalizedMode,
      pose: activePoseName,
      threeVersion: THREE.REVISION,
      viewport: {
        width: VIEWPORT_WIDTH,
        height: VIEWPORT_HEIGHT,
        devicePixelRatio: 1
      },
      camera: {
        position: [...camera.position.toArray()],
        lookAt: [...activeLookAt],
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
        rackId: faultSpec.rackId,
        worldCenter: [...faultSpec.center],
        size: [...faultSpec.size],
        projectedPixelCenter: projectedCenter,
        projectedPixelBBox: projectedBBox,
        peakTempC: faultSpec.peakTempC,
        sigmaM: faultSpec.sigmaM
      },
      projected_cable_corridors: projectedCorridors,
      hud_exclusion_bboxes_px: activeSpecObj ? [[0, 0, 360, 64]] : [],
      edges: activeState.edgeMap || null,
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
        peakTempC: faultSpec.peakTempC,
        sigmaM: faultSpec.sigmaM
      }
    };

    window.__BLACKBOX_RENDER_META__ = Object.freeze(meta);
    return meta;
  }

  window.setRenderMode = (mode) => applyModeAndRender(mode);

  window.setCameraPose = (position, lookAt = REFERENCE_CAMERA_SPEC.lookAt) => {
    camera.position.set(position[0], position[1], position[2]);
    activeLookAt = [lookAt[0], lookAt[1], lookAt[2]];
    camera.lookAt(activeLookAt[0], activeLookAt[1], activeLookAt[2]);
    const currentMode = window.__BLACKBOX_RENDER_META__?.mode || initialMode;
    return applyModeAndRender(currentMode);
  };

  window.renderScene = (mode = 'rgb', pose = 'rear_inspection', specObj = null) => {
    if (specObj && typeof specObj === 'object') {
      activeSpecObj = specObj;
      activeState = createParameterizedSceneGraph(specObj);
    }
    const poseKey = pose && CAMERA_POSES[pose] ? pose : 'rear_inspection';
    activePoseName = poseKey;
    const poseSpec = CAMERA_POSES[poseKey];
    camera.position.set(...poseSpec.position);
    activeLookAt = [...poseSpec.lookAt];
    camera.lookAt(...activeLookAt);
    return applyModeAndRender(mode);
  };

  window.getMetadata = () => window.__BLACKBOX_RENDER_META__;

  applyModeAndRender(initialMode);
  window.__BLACKBOX_SCENE_READY__ = true;
  window.READY = true;
}

if (typeof window !== 'undefined') {
  initBlackboxScene();
}
