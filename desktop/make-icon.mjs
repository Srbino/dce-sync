/**
 * Rasterise desktop/icon.svg into the PNG ladder and a macOS .icns.
 *
 *   node desktop/make-icon.mjs
 *
 * Only needed when the artwork changes — icon.icns is committed, so installing
 * the desktop app does not require node at all.
 *
 * Chromium (via playwright) does the rasterising: it is the one renderer that
 * handles the gradient, the mask and the stroke geometry exactly the way the
 * SVG is authored. playwright is not a dependency of this repo, so point
 * $DCE_PLAYWRIGHT_FROM at any project that has it installed.
 */
import { createRequire } from "node:module";
import { mkdir, copyFile, readFile, rm } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const run = promisify(execFile);
const HERE = dirname(fileURLToPath(import.meta.url));
const ICONS_DIR = join(HERE, "icons");
const SIZES = [16, 32, 64, 128, 256, 512, 1024];

function loadChromium() {
  const hint = process.env.DCE_PLAYWRIGHT_FROM;
  const roots = [
    ...(hint ? [join(resolve(hint), "package.json")] : []),
    join(HERE, "package.json"),
  ];
  for (const root of roots) {
    try {
      return createRequire(root)("playwright").chromium;
    } catch { /* try the next root */ }
  }
  throw new Error(
    "playwright not found — set DCE_PLAYWRIGHT_FROM to a project that has it,\n" +
    "  e.g. DCE_PLAYWRIGHT_FROM=../uo-outlands-vendor-investment node desktop/make-icon.mjs",
  );
}

async function rasterise(svg) {
  const chromium = loadChromium();
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const dataUrl = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
  const written = [];
  for (const size of SIZES) {
    await page.setViewportSize({ width: size, height: size });
    await page.setContent(
      `<body style="margin:0;background:transparent">
         <img src="${dataUrl}" style="width:${size}px;height:${size}px;display:block">
       </body>`,
    );
    const file = join(ICONS_DIR, `icon-${size}.png`);
    await page.screenshot({ path: file, omitBackground: true });
    written.push({ size, file });
    console.log(`[icon] icon-${size}.png`);
  }
  await browser.close();
  return written;
}

/** iconutil wants this exact naming; @2x is the double-resolution variant. */
async function buildIcns(pngs) {
  const iconset = join(ICONS_DIR, "OutlandsDiscord.iconset");
  await mkdir(iconset, { recursive: true });
  const layout = [
    [16, "icon_16x16.png"], [32, "icon_16x16@2x.png"],
    [32, "icon_32x32.png"], [64, "icon_32x32@2x.png"],
    [128, "icon_128x128.png"], [256, "icon_128x128@2x.png"],
    [256, "icon_256x256.png"], [512, "icon_256x256@2x.png"],
    [512, "icon_512x512.png"], [1024, "icon_512x512@2x.png"],
  ];
  const bySize = new Map(pngs.map((p) => [p.size, p.file]));
  for (const [size, name] of layout) {
    await copyFile(bySize.get(size), join(iconset, name));
  }
  const icns = join(HERE, "icon.icns");
  await run("iconutil", ["-c", "icns", iconset, "-o", icns]);
  await rm(iconset, { recursive: true, force: true });
  return icns;
}

await mkdir(ICONS_DIR, { recursive: true });
const svg = await readFile(join(HERE, "icon.svg"), "utf8");
const icns = await buildIcns(await rasterise(svg));
console.log(`[icon] ${icns}`);
