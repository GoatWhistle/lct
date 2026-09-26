import { readdir, readFile, stat, utimes, writeFile } from "node:fs/promises";
import { gzip } from "node:zlib";
import { promisify } from "node:util";

const gzipAsync = promisify(gzip);
const assetDirectory = new URL("../dist/assets/", import.meta.url);
const compressedExtensions = /\.(?:js|css|json|svg)$/i;
const minimumBytes = 1024;
let compressedCount = 0;
let sourceBytes = 0;
let gzipBytes = 0;

async function compressAssets(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
    if (entry.isDirectory()) {
      await compressAssets(path);
      continue;
    }
    if (!compressedExtensions.test(entry.name)) continue;

    const info = await stat(path);
    if (info.size < minimumBytes) continue;
    const source = await readFile(path);
    const compressed = await gzipAsync(source, { level: 9 });
    const compressedPath = new URL(`${entry.name}.gz`, directory);
    await writeFile(compressedPath, compressed);
    // Nginx gzip_static can safely pair the sidecar with its source file.
    await utimes(compressedPath, info.atime, info.mtime);
    compressedCount += 1;
    sourceBytes += source.byteLength;
    gzipBytes += compressed.byteLength;
  }
}

await compressAssets(assetDirectory);
console.log(
  `Precompressed ${compressedCount} assets: ${sourceBytes} -> ${gzipBytes} bytes`,
);
