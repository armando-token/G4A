import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DEFAULT_EXECUTABLE_CANDIDATES = [
  process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  process.env.CHROME_BIN,
  '/usr/local/bin/chromium',
  '/usr/local/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable'
].filter(Boolean);

const MIME_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png'
};

function startStaticServer(rootDir) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(async (req, res) => {
      try {
        const parsed = new URL(req.url, 'http://127.0.0.1');
        let relPath = decodeURIComponent(parsed.pathname);
        if (relPath === '/') relPath = '/scene.html';
        const safePath = path.normalize(path.join(rootDir, relPath));
        if (!safePath.startsWith(rootDir)) {
          res.writeHead(403);
          res.end('Forbidden');
          return;
        }
        const data = await fs.readFile(safePath);
        const ext = path.extname(safePath);
        res.writeHead(200, { 'Content-Type': MIME_TYPES[ext] || 'application/octet-stream' });
        res.end(data);
      } catch {
        res.writeHead(404);
        res.end('Not found');
      }
    });
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const addr = server.address();
      resolve({ server, baseUrl: `http://127.0.0.1:${addr.port}` });
    });
  });
}

async function resolveBrowserExecutable() {
  for (const candidate of DEFAULT_EXECUTABLE_CANDIDATES) {
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      // continue checking candidates
    }
  }
  return undefined;
}

function parseCliArgs(argv) {
  const args = {
    outDir: path.join(__dirname, 'renders'),
    pose: 'aisle_overview',
    width: 1280,
    height: 720,
    n2TempC: 48.5,
    n3TempC: 84.2,
    modes: ['rgb', 'thermal']
  };

  for (let i = 2; i < argv.length; i++) {
    const token = argv[i];
    if ((token === '--out-dir' || token === '--out') && argv[i + 1]) {
      args.outDir = path.resolve(argv[++i]);
    } else if (token === '--pose' && argv[i + 1]) {
      args.pose = argv[++i];
    } else if (token === '--width' && argv[i + 1]) {
      args.width = parseInt(argv[++i], 10);
    } else if (token === '--height' && argv[i + 1]) {
      args.height = parseInt(argv[++i], 10);
    } else if (token === '--n2-temp' && argv[i + 1]) {
      args.n2TempC = parseFloat(argv[++i]);
    } else if (token === '--n3-temp' && argv[i + 1]) {
      args.n3TempC = parseFloat(argv[++i]);
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
  const executablePath = await resolveBrowserExecutable();

  const launchOptions = {
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
  };
  if (executablePath) {
    launchOptions.executablePath = executablePath;
  }

  const { server, baseUrl } = await startStaticServer(__dirname);
  const browser = await chromium.launch(launchOptions);
  const results = [];

  try {
    const context = await browser.newContext({
      viewport: { width: config.width, height: config.height },
      deviceScaleFactor: 1
    });
    const page = await context.newPage();

    for (const mode of config.modes) {
      const url = new URL(`${baseUrl}/scene.html`);
      url.searchParams.set('mode', mode);
      url.searchParams.set('pose', config.pose);
      url.searchParams.set('width', String(config.width));
      url.searchParams.set('height', String(config.height));
      url.searchParams.set('n2TempC', String(config.n2TempC));
      url.searchParams.set('n3TempC', String(config.n3TempC));

      await page.goto(url.toString(), { waitUntil: 'load' });
      await page.waitForFunction(() => window.__BLACKBOX_SCENE_READY__ === true, null, {
        timeout: 15000
      });

      const outPath = path.join(config.outDir, `rear_aisle_${config.pose}_${mode}.png`);
      await page.screenshot({
        path: outPath,
        type: 'png',
        clip: { x: 0, y: 0, width: config.width, height: config.height }
      });

      const meta = await page.evaluate(() => window.__BLACKBOX_RENDER_META__);
      results.push({ mode, outPath, meta });
    }
  } finally {
    await browser.close();
    server.close();
  }

  return results;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  captureSceneRenders()
    .then((renders) => {
      console.log(JSON.stringify({ status: 'ok', renders }, null, 2));
    })
    .catch((err) => {
      console.error('Failed to capture 3D scene renders:', err);
      process.exitCode = 1;
    });
}
