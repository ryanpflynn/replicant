// Copies src/ to dist/ (the console bundles it with its own toolchain; this keeps `pnpm run build` dependency-free).
import { cpSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });
cpSync("src", "dist", { recursive: true });
console.log("built dist/");
