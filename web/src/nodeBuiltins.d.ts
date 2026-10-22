// Minimal ambient declarations for the Node builtins the web test
// suite's source-level guard tests read: those tests read sources and
// fixtures off disk, and one of them hashes a fixture to name the
// locator the demo seed pushes. The web toolchain ships no
// ``@types/node`` and the spec forbids adding a dependency for a test
// helper, so this declares only the small surface those tests import.

declare module "node:fs" {
  export interface Dirent {
    name: string;
    isDirectory(): boolean;
  }
  export function readFileSync(path: string, encoding: "utf8"): string;
  export function readdirSync(
    path: string,
    options: { withFileTypes: true },
  ): Dirent[];
}

declare module "node:path" {
  export function join(...parts: string[]): string;
  export function dirname(path: string): string;
}

declare module "node:url" {
  export function fileURLToPath(url: string | URL): string;
}

declare module "node:crypto" {
  export interface Hash {
    update(data: string): Hash;
    digest(encoding: "hex"): string;
  }
  export function createHash(algorithm: string): Hash;
}
