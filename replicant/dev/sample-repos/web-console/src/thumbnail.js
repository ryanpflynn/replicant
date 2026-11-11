// Invoice PDF thumbnails are rendered with the libvips CLI (`vips thumbnail`), which the console's CI image provides.
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

export async function thumbnail(pdfPath, outPath, width = 240) {
  await run("vips", ["thumbnail", pdfPath, outPath, String(width)]);
  return outPath;
}
