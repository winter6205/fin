// Minimal ambient declarations for the Node builtins the source-level
// chrome guard test (``productChrome.test.ts``) reads. The web
// toolchain ships no ``@types/node`` and the spec forbids adding a
// dependency for a test helper, so this declares only the surface that
// test imports.

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
