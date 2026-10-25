import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import zlib from 'node:zlib';
import { chromium } from 'playwright-core';

const execFileAsync = promisify(execFile);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const CHROME_CANDIDATES = [
  process.env.CHROME_EXECUTABLE_PATH,
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  process.env.CHROME_BIN,
  '/usr/local/bin/chromium',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable'
].filter(Boolean);

async function resolveChromeExecutable() {
  for (const candidate of CHROME_CANDIDATES) {
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      // continue checking
    }
  }
  throw new Error(
    `No Chromium/Chrome executable found. Checked: ${CHROME_CANDIDATES.join(', ')}. ` +
      'Set CHROME_EXECUTABLE_PATH to a valid browser binary.'
  );
}

/**
 * Compute CRC32 for PNG chunks.
 */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) {
    c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function makePngChunk(typeStr, dataBuf) {
  const typeBuf = Buffer.from(typeStr, 'ascii');
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(dataBuf.length, 0);
  const crcInput = Buffer.concat([typeBuf, dataBuf]);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(crcInput), 0);
  return Buffer.concat([lenBuf, typeBuf, dataBuf, crcBuf]);
}

function paethPredictor(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

/**
 * Convert an RGBA (colorType 6) or RGB (colorType 2) PNG Buffer from Playwright
 * into a compact 24-bit RGB PNG Buffer (colorType = 2, bitDepth = 8, no alpha channel).
 */
function ensure24BitRgbPng(pngBuffer) {
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (pngBuffer.subarray(0, 8).compare(sig) !== 0) {
    throw new Error('Invalid PNG signature');
  }

  let offset = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 8;
  let colorType = 6;
  const idatChunks = [];

  while (offset < pngBuffer.length) {
    const length = pngBuffer.readUInt32BE(offset);
    const type = pngBuffer.subarray(offset + 4, offset + 8).toString('ascii');
    const data = pngBuffer.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data.readUInt8(8);
      colorType = data.readUInt8(9);
    } else if (type === 'IDAT') {
      idatChunks.push(data);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + length;
  }

  if (colorType === 2 && bitDepth === 8 && pngBuffer.length < 512 * 1024) {
    return pngBuffer;
  }
  if (colorType !== 6 && colorType !== 2) {
    throw new Error(`Unsupported PNG colorType ${colorType}`);
  }

  const bpp = colorType === 6 ? 4 : 3;
  const stride = width * bpp;
  const inflated = zlib.inflateSync(Buffer.concat(idatChunks));

  const recon = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const filterType = inflated[y * (stride + 1)];
    const rowInOff = y * (stride + 1) + 1;
    const rowOutOff = y * stride;
    const prevOutOff = (y - 1) * stride;

    for (let x = 0; x < stride; x++) {
      const raw = inflated[rowInOff + x];
      const a = x >= bpp ? recon[rowOutOff + x - bpp] : 0;
      const b = y > 0 ? recon[prevOutOff + x] : 0;
      const c = x >= bpp && y > 0 ? recon[prevOutOff + x - bpp] : 0;

      let val = raw;
      if (filterType === 1) val = (raw + a) & 0xff;
      else if (filterType === 2) val = (raw + b) & 0xff;
      else if (filterType === 3) val = (raw + ((a + b) >> 1)) & 0xff;
      else if (filterType === 4) val = (raw + paethPredictor(a, b, c)) & 0xff;
      recon[rowOutOff + x] = val;
    }
  }

  // Re-encode as 24-bit RGB (3 bytes per pixel) using Sub filter (1) for small size < 512 KiB
  const outStride = width * 3;
  const rawRgbScanlines = Buffer.alloc(height * (outStride + 1));

  for (let y = 0; y < height; y++) {
    const inRow = y * stride;
    const outRow = y * (outStride + 1);
    rawRgbScanlines[outRow] = 1; // Sub filter
    for (let px = 0; px < width; px++) {
      const r = recon[inRow + px * bpp];
      const g = recon[inRow + px * bpp + 1];
      const b = recon[inRow + px * bpp + 2];

      const prevR = px > 0 ? recon[inRow + (px - 1) * bpp] : 0;
      const prevG = px > 0 ? recon[inRow + (px - 1) * bpp + 1] : 0;
      const prevB = px > 0 ? recon[inRow + (px - 1) * bpp + 2] : 0;

      rawRgbScanlines[outRow + 1 + px * 3] = (r - prevR) & 0xff;
      rawRgbScanlines[outRow + 1 + px * 3 + 1] = (g - prevG) & 0xff;
      rawRgbScanlines[outRow + 1 + px * 3 + 2] = (b - prevB) & 0xff;
    }
  }

  const compressedIdat = zlib.deflateSync(rawRgbScanlines, { level: 9 });
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr.writeUInt8(8, 8); // bitDepth = 8
  ihdr.writeUInt8(2, 9); // colorType = 2 (Truecolor 24-bit RGB)
  ihdr.writeUInt8(0, 10);
  ihdr.writeUInt8(0, 11);
  ihdr.writeUInt8(0, 12);

  return Buffer.concat([
    sig,
    makePngChunk('IHDR', ihdr),
    makePngChunk('IDAT', compressedIdat),
    makePngChunk('IEND', Buffer.alloc(0))
  ]);
}

/**
 * Start a minimal local static HTTP server rooted at static3d directory.
 */
async function startLocalStaticServer(rootDir) {
  const rootDirResolved = path.resolve(rootDir);
  const mimeTypes = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png'
  };

  const server = http.createServer(async (req, res) => {
    try {
      const reqUrl = new URL(req.url || '/', 'http://127.0.0.1');
      const relPath = decodeURIComponent(reqUrl.pathname === '/' ? '/scene.html' : reqUrl.pathname);
      const absPath = path.resolve(rootDirResolved, '.' + relPath);
      const rel = path.relative(rootDirResolved, absPath);
      if (rel.startsWith('..') || path.isAbsolute(rel)) {
        res.writeHead(403);
        res.end('Forbidden');
        return;
      }
      const data = await fs.readFile(absPath);
      const ext = path.extname(absPath).toLowerCase();
      res.writeHead(200, { 'Content-Type': mimeTypes[ext] || 'application/octet-stream' });
      res.end(data);
    } catch {
      res.writeHead(404);
      res.end('Not found');
    }
  });

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  return {
    server,
    baseUrl: `http://127.0.0.1:${address.port}`
  };
}

/**
 * Atomically write a 24-bit RGB PNG (< 512 KiB) via temporary file + rename.
 */
async function writeAtomic24BitPng(rawPngBuf, finalPath) {
  const rgb24Buf = ensure24BitRgbPng(rawPngBuf);
  if (rgb24Buf.length >= 512 * 1024) {
    throw new Error(`Captured PNG exceeds 512 KiB budget (${rgb24Buf.length} bytes): ${finalPath}`);
  }
  await fs.mkdir(path.dirname(finalPath), { recursive: true });
  const tmpPath = `${finalPath}.${process.pid}.${Date.now()}.tmp.png`;
  await fs.writeFile(tmpPath, rgb24Buf);
  await fs.rename(tmpPath, finalPath);
  const sha256 = crypto.createHash('sha256').update(rgb24Buf).digest('hex');
  return { path: finalPath, bytes: rgb24Buf.length, sha256 };
}

function parseCliArgs(argv) {
  const args = {
    specPath: null,
    outDir: path.join(__dirname, 'renders'),
    rgbOut: null,
    thermalOut: null,
    metaOut: null,
    approachClip: null,
    pose: null,
    width: 1280,
    height: 800,
    modes: ['rgb', 'thermal']
  };

  if (argv[2] === 'scene' && argv[3] && argv[4]) {
    args.specPath = path.resolve(argv[3]);
    args.outDir = path.resolve(argv[4]);
    return args;
  }

  if (argv.length === 3 && argv[2] && !argv[2].startsWith('-')) {
    args.outDir = path.resolve(argv[2]);
    return args;
  }

  for (let i = 2; i < argv.length; i++) {
    const token = argv[i];
    if (token === '--spec' && argv[i + 1]) {
      args.specPath = path.resolve(argv[++i]);
    } else if (token === '--out-dir' && argv[i + 1]) {
      args.outDir = path.resolve(argv[++i]);
    } else if (token === '--rgb-out' && argv[i + 1]) {
      args.rgbOut = path.resolve(argv[++i]);
    } else if (token === '--thermal-out' && argv[i + 1]) {
      args.thermalOut = path.resolve(argv[++i]);
    } else if (token === '--meta-out' && argv[i + 1]) {
      args.metaOut = path.resolve(argv[++i]);
    } else if (token === '--approach-clip' && argv[i + 1]) {
      args.approachClip = path.resolve(argv[++i]);
    } else if (token === '--pose' && argv[i + 1]) {
      args.pose = argv[++i];
    } else if (token === '--width' && argv[i + 1]) {
      args.width = parseInt(argv[++i], 10);
    } else if (token === '--height' && argv[i + 1]) {
      args.height = parseInt(argv[++i], 10);
    } else if (token === '--mode' && argv[i + 1]) {
      args.modes = [argv[++i]];
    }
  }
  return args;
}

export async function captureSceneRenders(options = {}) {
  const config = {
    ...parseCliArgs(process.argv),
    ...options
  };

  await fs.mkdir(config.outDir, { recursive: true });
  const executablePath = await resolveChromeExecutable();
  const { server, baseUrl } = await startLocalStaticServer(__dirname);

  let browser = null;
  try {
    browser = await chromium.launch({
      executablePath,
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--use-gl=angle',
        '--use-angle=swiftshader',
        '--enable-webgl',
        '--ignore-gpu-blocklist'
      ]
    });

    const browserVersion = browser.version();
    const context = await browser.newContext({
      viewport: { width: config.width, height: config.height },
      deviceScaleFactor: 1
    });
    const page = await context.newPage();

    await page.goto(`${baseUrl}/scene.html`, { waitUntil: 'load' });
    await page.waitForFunction(
      () => window.__BLACKBOX_SCENE_READY__ === true,
      null,
      { timeout: 15000 }
    );
    await page.evaluate(() => document.fonts.ready);

    // Parameterized SceneSpec v1 capture path
    if (config.specPath) {
      const specBytes = await fs.readFile(config.specPath);
      const specSha256 = crypto.createHash('sha256').update(specBytes).digest('hex');
      const specObj = JSON.parse(specBytes.toString('utf-8'));

      // 1. render_wiring.png (rgb mode, wiring_oblique pose)
      const metaWiring = await page.evaluate(
        (s) => window.renderScene('rgb', 'wiring_oblique', s),
        specObj
      );
      const rawWiring = await page.screenshot({
        type: 'png',
        clip: { x: 0, y: 0, width: config.width, height: config.height }
      });
      const savedWiring = await writeAtomic24BitPng(
        rawWiring,
        path.join(config.outDir, 'render_wiring.png')
      );

      // 2. render_rgb_rear.png (rgb mode, rear_inspection pose)
      const metaRgbRear = await page.evaluate(
        () => window.renderScene('rgb', 'rear_inspection', null)
      );
      const rawRgbRear = await page.screenshot({
        type: 'png',
        clip: { x: 0, y: 0, width: config.width, height: config.height }
      });
      const savedRgbRear = await writeAtomic24BitPng(
        rawRgbRear,
        path.join(config.outDir, 'render_rgb_rear.png')
      );

      // 3. render_thermal_rear.png (thermal mode, rear_inspection pose)
      const metaThermalRear = await page.evaluate(
        () => window.renderScene('thermal', 'rear_inspection', null)
      );
      const rawThermalRear = await page.screenshot({
        type: 'png',
        clip: { x: 0, y: 0, width: config.width, height: config.height }
      });
      const savedThermalRear = await writeAtomic24BitPng(
        rawThermalRear,
        path.join(config.outDir, 'render_thermal_rear.png')
      );

      const renderMetaPayload = {
        status: 'ok',
        webgl_ok: true,
        schema_version: 'render_meta.v1',
        spec_sha256: specSha256,
        source_revision: String(specObj.source_revision || 'A'),
        case_id: String(specObj.case_id || ''),
        edges: metaWiring.edges,
        projected_cable_corridors: metaWiring.projected_cable_corridors,
        hud_exclusion_bboxes_px: metaWiring.hud_exclusion_bboxes_px || [[0, 0, 360, 64]],
        hotspot_roi: metaRgbRear.faultConnector,
        camera_matrices: {
          wiring_oblique: metaWiring.camera,
          rear_inspection: metaRgbRear.camera
        },
        renders: {
          'render_wiring.png': savedWiring,
          'render_rgb_rear.png': savedRgbRear,
          'render_thermal_rear.png': savedThermalRear
        },
        browserVersion,
        executablePath
      };

      const metaFilePath = config.metaOut || path.join(config.outDir, 'render_meta.json');
      const tmpMeta = `${metaFilePath}.${process.pid}.tmp.json`;
      await fs.writeFile(tmpMeta, JSON.stringify(renderMetaPayload, null, 2) + '\n', 'utf-8');
      await fs.rename(tmpMeta, metaFilePath);
      return renderMetaPayload;
    }

    const renders = [];
    const metadataByMode = {};

    for (const mode of config.modes) {
      const meta = await page.evaluate((m) => window.setRenderMode(m), mode);
      metadataByMode[mode] = meta;

      const rawShot = await page.screenshot({
        type: 'png',
        clip: { x: 0, y: 0, width: config.width, height: config.height }
      });

      const primaryPath =
        mode === 'rgb' && config.rgbOut
          ? config.rgbOut
          : mode === 'thermal' && config.thermalOut
            ? config.thermalOut
            : path.join(config.outDir, `rear_aisle_${mode}.png`);

      const saved = await writeAtomic24BitPng(rawShot, primaryPath);

      if (config.pose) {
        const posePath = path.join(config.outDir, `rear_aisle_${config.pose}_${mode}.png`);
        if (posePath !== primaryPath) {
          await writeAtomic24BitPng(rawShot, posePath);
        }
      }

      renders.push({
        mode,
        outPath: saved.path,
        bytes: saved.bytes,
        sha256: saved.sha256,
        meta
      });
    }

    let approachClipInfo = null;
    if (config.approachClip) {
      const framesDir = path.join(
        path.dirname(config.approachClip),
        `.frames_${process.pid}_${Date.now()}`
      );
      await fs.mkdir(framesDir, { recursive: true });
      try {
        await page.evaluate(() => window.setRenderMode('rgb'));
        const totalFrames = 24;
        const startPos = [1.23, 1.35, 4.45];
        const endPos = [1.23, 1.24, 3.45];

        for (let f = 0; f < totalFrames; f++) {
          const t = f / (totalFrames - 1);
          const pos = [
            startPos[0] + (endPos[0] - startPos[0]) * t,
            startPos[1] + (endPos[1] - startPos[1]) * t,
            startPos[2] + (endPos[2] - startPos[2]) * t
          ];
          await page.evaluate((p) => window.setCameraPose(p), pos);
          const frameShot = await page.screenshot({
            type: 'png',
            clip: { x: 0, y: 0, width: config.width, height: config.height }
          });
          const framePath = path.join(framesDir, `frame_${String(f).padStart(3, '0')}.png`);
          await writeAtomic24BitPng(frameShot, framePath);
        }

        const tmpMp4 = `${config.approachClip}.${process.pid}.tmp.mp4`;
        await execFileAsync('ffmpeg', [
          '-y',
          '-framerate',
          '12',
          '-i',
          path.join(framesDir, 'frame_%03d.png'),
          '-c:v',
          'libx264',
          '-pix_fmt',
          'yuv420p',
          '-movflags',
          '+faststart',
          tmpMp4
        ]);
        await fs.rename(tmpMp4, config.approachClip);
        const clipBytes = await fs.readFile(config.approachClip);
        approachClipInfo = {
          path: config.approachClip,
          bytes: clipBytes.length,
          sha256: crypto.createHash('sha256').update(clipBytes).digest('hex'),
          frames: totalFrames,
          fps: 12
        };
      } finally {
        await fs.rm(framesDir, { recursive: true, force: true });
      }
    }

    const resultPayload = {
      status: 'ok',
      browserVersion,
      executablePath,
      renders,
      approachClip: approachClipInfo,
      metadataByMode
    };

    const metaTarget = config.metaOut || path.join(config.outDir, 'render_meta.json');
    await fs.mkdir(path.dirname(metaTarget), { recursive: true });
    const tmpMeta = `${metaTarget}.${process.pid}.tmp.json`;
    await fs.writeFile(tmpMeta, JSON.stringify(resultPayload, null, 2) + '\n', 'utf-8');
    await fs.rename(tmpMeta, metaTarget);

    return resultPayload;
  } finally {
    if (browser) {
      await browser.close();
    }
    await new Promise((resolve) => server.close(resolve));
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  captureSceneRenders()
    .then((payload) => {
      console.log(JSON.stringify(payload, null, 2));
    })
    .catch((err) => {
      console.error('Failed to capture 3D scene renders:', err);
      process.exitCode = 1;
    });
}
